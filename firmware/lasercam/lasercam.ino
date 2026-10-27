/*
 * lasercam — camera d'alignement montee sur la tete du laser.
 * Cible : Joy-IT SBC-ESP32-Cam (clone AI-Thinker : OV2640 + 4 Mo de PSRAM).
 *
 * DEUX MODES, decides au demarrage :
 *
 * 1. STATION — la carte rejoint le WiFi memorise. C'est le mode de travail :
 *    laser_pcb la trouve en mDNS sous <hostname>.local.
 *
 * 2. POINT D'ACCES — aucun reseau memorise, ou injoignable. La carte ouvre son
 *    propre reseau `lasercam-setup` avec un PORTAIL CAPTIF : le telephone ou le
 *    PC qui s'y connecte voit la page de configuration s'ouvrir toute seule.
 *    La camera reste entierement utilisable dans ce mode (sur 192.168.4.1) —
 *    c'est le mode « sans reseau » : on peut viser une piece sur le terrain,
 *    sans box ni routeur.
 *
 * Le reglage est en NVS (`Preferences`), pas dans le code : on configure la
 * carte sans la reflasher. `secrets.h`, s'il existe, sert seulement d'amorce au
 * tout premier demarrage.
 *
 * Routes (port 80, sauf le flux) :
 *   /                page de controle minimale
 *   /status          JSON (mode, identification, reglages, etat)
 *   /capture         une image JPEG
 *   /control?var=&val=   framesize, quality, hmirror, vflip, ...
 *   /laser?on=0|1[&ms=]  croix laser (GPIO13)
 *   /led?v=0..255    LED blanche de la carte
 *   /wifi            page de configuration (les deux modes)
 *   /save  (POST)    enregistre le reseau et redemarre
 *   /forget          oublie le reseau et redemarre sur le portail
 *   :81/stream       flux MJPEG (multipart/x-mixed-replace)
 *
 * Le flux est sur un SECOND serveur : le httpd d'ESP-IDF traite une requete a
 * la fois, et un flux MJPEG ne se termine jamais — sur un serveur unique il
 * bloquerait /laser et /control pour toujours.
 *
 * SECURITE. La croix laser s'eteint toute seule apres LASER_TIMEOUT_MS sans
 * nouvelle commande : une liaison WiFi qui tombe ne doit pas laisser un
 * faisceau allume sur la machine. Elle est aussi eteinte AVANT le WiFi et la
 * camera au demarrage, pour que le reset ne l'allume pas.
 */
#include "esp_camera.h"
#include "esp_http_server.h"
#include <WiFi.h>
#include <ESPmDNS.h>
#include <DNSServer.h>
#include <Preferences.h>

// --------------------------------------------------------------------------- //
// Amorce optionnelle : `secrets.h` ne sert qu'au PREMIER demarrage, quand la
// NVS est encore vide. Ensuite c'est le portail qui commande. Le fichier est
// ignore par git : le mot de passe du reseau n'a rien a faire dans un depot.
// --------------------------------------------------------------------------- //
#if __has_include("secrets.h")
#include "secrets.h"
#endif
#ifndef WIFI_SSID_VALUE
#define WIFI_SSID_VALUE ""
#define WIFI_PASS_VALUE ""
#endif
#ifndef HOSTNAME_VALUE
#define HOSTNAME_VALUE "lasercam"
#endif

// Reseau du portail captif.
static const char *AP_SSID = "lasercam-setup";
static const char *AP_PASS = "laserpcb";         // 8 caracteres minimum

static const uint32_t WIFI_TIMEOUT_MS  = 20000;
static const uint32_t LASER_TIMEOUT_MS = 120000; // extinction de securite
static const int      FW_VERSION       = 2;

// --------------------------------------------------------------------------- //
// Brochage AI-Thinker / Joy-IT SBC-ESP32-Cam
// --------------------------------------------------------------------------- //
#define PWDN_GPIO_NUM   32
#define RESET_GPIO_NUM  -1
#define XCLK_GPIO_NUM    0
#define SIOD_GPIO_NUM   26
#define SIOC_GPIO_NUM   27
#define Y9_GPIO_NUM     35
#define Y8_GPIO_NUM     34
#define Y7_GPIO_NUM     39
#define Y6_GPIO_NUM     36
#define Y5_GPIO_NUM     21
#define Y4_GPIO_NUM     19
#define Y3_GPIO_NUM     18
#define Y2_GPIO_NUM      5
#define VSYNC_GPIO_NUM  25
#define HREF_GPIO_NUM   23
#define PCLK_GPIO_NUM   22

// GPIO13 : seule broche vraiment libre une fois la camera active, et sans role
// de strapping au demarrage. GPIO12 (MTDI) et GPIO15 (MTDO) en ont un, GPIO16
// porte la PSRAM, GPIO14/15/2 servent a la carte SD.
#define LASER_PIN 13
#define LED_PIN    4                             // LED blanche (aussi SD D1)
#define LED_CHANNEL 7

static bool     laser_on    = false;
static uint32_t laser_since = 0;                 // millis() du dernier ordre
static uint32_t laser_hold  = LASER_TIMEOUT_MS;  // duree demandee
static uint8_t  led_level   = 0;

static httpd_handle_t control_httpd = NULL;
static httpd_handle_t stream_httpd  = NULL;

static bool      portal_mode = false;            // point d'acces + portail
static DNSServer dns;
static Preferences prefs;
static uint32_t  reboot_at = 0;                  // redemarrage differe

static String cfg_ssid, cfg_pass, cfg_host;

// --------------------------------------------------------------------------- //
// Configuration persistante (NVS)
// --------------------------------------------------------------------------- //
static void cfg_load() {
  prefs.begin("lasercam", true);                 // lecture seule
  cfg_ssid = prefs.getString("ssid", "");
  cfg_pass = prefs.getString("pass", "");
  cfg_host = prefs.getString("host", "");
  prefs.end();
  if (cfg_ssid.isEmpty() && strlen(WIFI_SSID_VALUE) > 0) {
    cfg_ssid = WIFI_SSID_VALUE;                  // amorce du premier demarrage
    cfg_pass = WIFI_PASS_VALUE;
  }
  if (cfg_host.isEmpty()) cfg_host = HOSTNAME_VALUE;
}

static void cfg_save(const String &ssid, const String &pass, const String &host) {
  prefs.begin("lasercam", false);
  prefs.putString("ssid", ssid);
  prefs.putString("pass", pass);
  prefs.putString("host", host.isEmpty() ? String(HOSTNAME_VALUE) : host);
  prefs.end();
}

static void cfg_forget() {
  prefs.begin("lasercam", false);
  prefs.clear();
  prefs.end();
}

// --------------------------------------------------------------------------- //
// Sorties : croix laser et LED
// --------------------------------------------------------------------------- //
static void led_write(uint8_t level) {
  led_level = level;
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcWrite(LED_PIN, level);
#else
  ledcWrite(LED_CHANNEL, level);
#endif
}

static void laser_set(bool on, uint32_t hold_ms) {
  laser_on = on;
  laser_hold = hold_ms ? hold_ms : LASER_TIMEOUT_MS;
  laser_since = millis();
  digitalWrite(LASER_PIN, on ? HIGH : LOW);
}

/* Coupe la croix si plus personne ne la rafraichit. Appelee depuis loop(). */
static void laser_watchdog() {
  if (laser_on && (millis() - laser_since) > laser_hold) {
    laser_on = false;
    digitalWrite(LASER_PIN, LOW);
  }
}

// --------------------------------------------------------------------------- //
// Camera
// --------------------------------------------------------------------------- //
static bool camera_start() {
  camera_config_t c = {};
  c.ledc_channel = LEDC_CHANNEL_0;
  c.ledc_timer   = LEDC_TIMER_0;
  c.pin_d0 = Y2_GPIO_NUM;  c.pin_d1 = Y3_GPIO_NUM;
  c.pin_d2 = Y4_GPIO_NUM;  c.pin_d3 = Y5_GPIO_NUM;
  c.pin_d4 = Y6_GPIO_NUM;  c.pin_d5 = Y7_GPIO_NUM;
  c.pin_d6 = Y8_GPIO_NUM;  c.pin_d7 = Y9_GPIO_NUM;
  c.pin_xclk = XCLK_GPIO_NUM;   c.pin_pclk  = PCLK_GPIO_NUM;
  c.pin_vsync = VSYNC_GPIO_NUM; c.pin_href  = HREF_GPIO_NUM;
  c.pin_sccb_sda = SIOD_GPIO_NUM; c.pin_sccb_scl = SIOC_GPIO_NUM;
  c.pin_pwdn = PWDN_GPIO_NUM;   c.pin_reset = RESET_GPIO_NUM;
  c.xclk_freq_hz = 20000000;
  c.pixel_format = PIXFORMAT_JPEG;
  c.grab_mode    = CAMERA_GRAB_LATEST;           // on veut l'image d'MAINTENANT
  c.fb_location  = CAMERA_FB_IN_PSRAM;

  if (psramFound()) {
    c.frame_size = FRAMESIZE_SVGA;               // 800x600 : bon compromis
    c.jpeg_quality = 12;
    c.fb_count = 2;
  } else {
    // Sans PSRAM le buffer tient en SRAM : il faut descendre, sinon l'init
    // echoue ou le flux se traine.
    c.frame_size = FRAMESIZE_QVGA;
    c.jpeg_quality = 15;
    c.fb_count = 1;
    c.fb_location = CAMERA_FB_IN_DRAM;
  }

  esp_err_t err = esp_camera_init(&c);
  if (err != ESP_OK) {
    Serial.printf("camera: init KO 0x%x\n", err);
    return false;
  }
  sensor_t *s = esp_camera_sensor_get();
  if (s) {
    // esp_camera_init ne renseigne pas status.framesize : sans cet appel
    // /status annonce une taille au hasard (vu : 11 = HD alors que le capteur
    // sortait bien du 800x600) et on debogue un fantome.
    s->set_framesize(s, c.frame_size);
    // L'OV2640 sort une image un peu plate ; on remonte le contraste pour
    // distinguer le cuivre du FR4, qui ont des reflets proches.
    s->set_contrast(s, 1);
    s->set_saturation(s, -1);
  }
  return true;
}

// --------------------------------------------------------------------------- //
// Reseau
// --------------------------------------------------------------------------- //
static bool sta_connect() {
  if (cfg_ssid.isEmpty()) {
    Serial.println("Aucun reseau memorise.");
    return false;
  }
  WiFi.persistent(false);
  WiFi.mode(WIFI_STA);
  WiFi.setHostname(cfg_host.c_str());
  WiFi.setSleep(false);                 // le sommeil WiFi hache le flux MJPEG
  Serial.printf("WiFi : connexion a %s", cfg_ssid.c_str());
  WiFi.begin(cfg_ssid.c_str(), cfg_pass.c_str());

  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && (millis() - t0) < WIFI_TIMEOUT_MS) {
    delay(250);
    Serial.print('.');
  }
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println(" KO");
    return false;
  }
  Serial.print(" OK -> "); Serial.println(WiFi.localIP());

  // laser_pcb resout directement l'enregistrement A (comme pour FluidNC, qui
  // n'annonce plus ses services) : le hostname suffit a la detection.
  if (MDNS.begin(cfg_host.c_str())) {
    MDNS.addService("http", "tcp", 80);
    Serial.printf("mDNS : %s.local\n", cfg_host.c_str());
  }
  return true;
}

static void portal_start() {
  // AP_STA, pas AP seul : le balayage des reseaux de la page de configuration a
  // besoin de l'interface station, et basculer de mode en cours de route
  // couperait le client en pleine configuration.
  WiFi.persistent(false);
  WiFi.mode(WIFI_AP_STA);
  WiFi.softAP(AP_SSID, AP_PASS);
  delay(100);
  IPAddress ip = WiFi.softAPIP();
  Serial.printf("Point d'acces %s (mot de passe %s) -> %s\n",
                AP_SSID, AP_PASS, ip.toString().c_str());

  // Le portail captif, c'est ce DNS : TOUT nom demande repond notre adresse, et
  // le systeme du client ouvre alors la page de configuration tout seul.
  dns.setErrorReplyCode(DNSReplyCode::NoError);
  dns.setTTL(0);
  dns.start(53, "*", ip);
  if (MDNS.begin(cfg_host.c_str())) MDNS.addService("http", "tcp", 80);
  portal_mode = true;
}

// --------------------------------------------------------------------------- //
// Utilitaires HTTP
// --------------------------------------------------------------------------- //
static int query_int(httpd_req_t *req, const char *key, int fallback) {
  char buf[64], val[16];
  size_t len = httpd_req_get_url_query_len(req) + 1;
  if (len <= 1 || len > sizeof(buf)) return fallback;
  if (httpd_req_get_url_query_str(req, buf, len) != ESP_OK) return fallback;
  if (httpd_query_key_value(buf, key, val, sizeof(val)) != ESP_OK) return fallback;
  return atoi(val);
}

static bool query_str(httpd_req_t *req, const char *key, char *out, size_t n) {
  char buf[64];
  size_t len = httpd_req_get_url_query_len(req) + 1;
  if (len <= 1 || len > sizeof(buf)) return false;
  if (httpd_req_get_url_query_str(req, buf, len) != ESP_OK) return false;
  return httpd_query_key_value(buf, key, out, n) == ESP_OK;
}

/* Decode le `application/x-www-form-urlencoded`.
 *
 * `httpd_query_key_value` ne touche PAS aux sequences `%xx` ni aux `+` : sans
 * ce decodage, tout mot de passe contenant une espace ou un caractere special
 * serait enregistre de travers — et la carte deviendrait injoignable sans
 * reflashage. */
static String url_decode(const String &in) {
  String out;
  out.reserve(in.length());
  for (size_t i = 0; i < in.length(); i++) {
    char ch = in[i];
    if (ch == '+') {
      out += ' ';
    } else if (ch == '%' && i + 2 < in.length()) {
      char hex[3] = {in[i + 1], in[i + 2], 0};
      out += (char)strtol(hex, nullptr, 16);
      i += 2;
    } else {
      out += ch;
    }
  }
  return out;
}

/* Valeur d'un champ dans un corps de formulaire (`a=1&b=2`). */
static String form_value(const String &body, const char *key) {
  String needle = String(key) + "=";
  int pos = -1;
  if (body.startsWith(needle)) {
    pos = needle.length();
  } else {
    int at = body.indexOf("&" + needle);
    if (at >= 0) pos = at + 1 + needle.length();
  }
  if (pos < 0) return String();
  int end = body.indexOf('&', pos);
  return url_decode(body.substring(pos, end < 0 ? body.length() : end));
}

static String html_escape(const String &in) {
  String out;
  out.reserve(in.length() + 8);
  for (size_t i = 0; i < in.length(); i++) {
    char ch = in[i];
    if      (ch == '&')  out += "&amp;";
    else if (ch == '<')  out += "&lt;";
    else if (ch == '>')  out += "&gt;";
    else if (ch == '"')  out += "&quot;";
    else if (ch == '\'') out += "&#39;";
    else out += ch;
  }
  return out;
}

// --------------------------------------------------------------------------- //
// Handlers : camera
// --------------------------------------------------------------------------- //
static esp_err_t status_handler(httpd_req_t *req) {
  sensor_t *s = esp_camera_sensor_get();
  String ip = portal_mode ? WiFi.softAPIP().toString()
                          : WiFi.localIP().toString();
  char json[420];
  snprintf(json, sizeof(json),
           "{\"fw\":\"lasercam\",\"version\":%d,\"mode\":\"%s\","
           "\"host\":\"%s\",\"ip\":\"%s\",\"ssid\":\"%s\",\"rssi\":%d,"
           "\"psram\":%d,\"framesize\":%d,\"quality\":%d,"
           "\"laser\":%d,\"led\":%d,\"uptime_s\":%lu}",
           FW_VERSION, portal_mode ? "ap" : "sta",
           cfg_host.c_str(), ip.c_str(),
           portal_mode ? AP_SSID : cfg_ssid.c_str(),
           portal_mode ? 0 : (int)WiFi.RSSI(), psramFound() ? 1 : 0,
           s ? (int)s->status.framesize : -1, s ? (int)s->status.quality : -1,
           laser_on ? 1 : 0, (int)led_level, (unsigned long)(millis() / 1000));
  httpd_resp_set_type(req, "application/json");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  return httpd_resp_send(req, json, strlen(json));
}

static esp_err_t capture_handler(httpd_req_t *req) {
  camera_fb_t *fb = esp_camera_fb_get();
  if (!fb) return httpd_resp_send_500(req);
  httpd_resp_set_type(req, "image/jpeg");
  httpd_resp_set_hdr(req, "Content-Disposition", "inline; filename=capture.jpg");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  esp_err_t res = httpd_resp_send(req, (const char *)fb->buf, fb->len);
  esp_camera_fb_return(fb);
  return res;
}

#define PART_BOUNDARY "lasercamframe"
static const char *STREAM_TYPE =
    "multipart/x-mixed-replace;boundary=" PART_BOUNDARY;
static const char *STREAM_BOUNDARY = "\r\n--" PART_BOUNDARY "\r\n";
static const char *STREAM_PART =
    "Content-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n";

static esp_err_t stream_handler(httpd_req_t *req) {
  esp_err_t res = httpd_resp_set_type(req, STREAM_TYPE);
  if (res != ESP_OK) return res;
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");

  char part[64];
  while (true) {
    camera_fb_t *fb = esp_camera_fb_get();
    if (!fb) { res = ESP_FAIL; break; }
    size_t n = snprintf(part, sizeof(part), STREAM_PART, fb->len);
    res = httpd_resp_send_chunk(req, STREAM_BOUNDARY, strlen(STREAM_BOUNDARY));
    if (res == ESP_OK) res = httpd_resp_send_chunk(req, part, n);
    if (res == ESP_OK) res = httpd_resp_send_chunk(req, (const char *)fb->buf, fb->len);
    esp_camera_fb_return(fb);
    if (res != ESP_OK) break;                    // le client a ferme : on sort
  }
  return res;
}

static esp_err_t laser_handler(httpd_req_t *req) {
  int on = query_int(req, "on", -1);
  int ms = query_int(req, "ms", 0);
  if (on < 0) {
    httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "on=0|1 attendu");
    return ESP_FAIL;
  }
  laser_set(on != 0, (uint32_t)ms);
  return status_handler(req);
}

static esp_err_t led_handler(httpd_req_t *req) {
  int v = query_int(req, "v", -1);
  if (v < 0 || v > 255) {
    httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "v=0..255 attendu");
    return ESP_FAIL;
  }
  led_write((uint8_t)v);
  return status_handler(req);
}

static esp_err_t control_handler(httpd_req_t *req) {
  char var[24];
  if (!query_str(req, "var", var, sizeof(var))) {
    httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "var= attendu");
    return ESP_FAIL;
  }
  int val = query_int(req, "val", 0);
  sensor_t *s = esp_camera_sensor_get();
  if (!s) return httpd_resp_send_500(req);

  int r = -1;
  if      (!strcmp(var, "framesize")) r = s->set_framesize(s, (framesize_t)val);
  else if (!strcmp(var, "quality"))   r = s->set_quality(s, val);
  else if (!strcmp(var, "contrast"))  r = s->set_contrast(s, val);
  else if (!strcmp(var, "brightness"))r = s->set_brightness(s, val);
  else if (!strcmp(var, "saturation"))r = s->set_saturation(s, val);
  else if (!strcmp(var, "hmirror"))   r = s->set_hmirror(s, val);
  else if (!strcmp(var, "vflip"))     r = s->set_vflip(s, val);
  else if (!strcmp(var, "gainceiling"))r = s->set_gainceiling(s, (gainceiling_t)val);
  else if (!strcmp(var, "awb"))       r = s->set_whitebal(s, val);
  else if (!strcmp(var, "aec"))       r = s->set_exposure_ctrl(s, val);
  else if (!strcmp(var, "agc"))       r = s->set_gain_ctrl(s, val);
  if (r != 0) {
    httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "reglage inconnu ou refuse");
    return ESP_FAIL;
  }
  return status_handler(req);
}

static esp_err_t index_handler(httpd_req_t *req) {
  // Page de secours : elle sert a verifier la carte depuis un navigateur,
  // sans laser_pcb. Le vrai client, c'est la GUI.
  static const char page[] =
      "<!doctype html><meta charset=utf-8><title>lasercam</title>"
      "<meta name=viewport content='width=device-width,initial-scale=1'>"
      "<style>body{font:14px sans-serif;margin:1em}img{max-width:100%}"
      "button{font-size:1em;margin:.2em}</style>"
      "<h3>lasercam</h3>"
      "<p><button onclick=\"fetch('/laser?on=1&ms=60000')\">Croix ON</button>"
      "<button onclick=\"fetch('/laser?on=0')\">Croix OFF</button>"
      "<button onclick=\"fetch('/led?v=60')\">Lumiere</button>"
      "<button onclick=\"fetch('/led?v=0')\">Noir</button>"
      "<a href=/wifi>Reseau</a></p>"
      "<img id=v><script>"
      "v.src='http://'+location.hostname+':81/stream';</script>";
  httpd_resp_set_type(req, "text/html");
  return httpd_resp_send(req, page, HTTPD_RESP_USE_STRLEN);
}

// --------------------------------------------------------------------------- //
// Handlers : configuration reseau
// --------------------------------------------------------------------------- //
static esp_err_t wifi_page_handler(httpd_req_t *req) {
  httpd_resp_set_type(req, "text/html");
  httpd_resp_sendstr_chunk(req,
      "<!doctype html><meta charset=utf-8><title>lasercam - reseau</title>"
      "<meta name=viewport content='width=device-width,initial-scale=1'>"
      "<style>body{font:15px sans-serif;margin:1em;max-width:30em}"
      "label{display:block;margin:.8em 0 .2em}"
      "input,select{width:100%;font-size:1em;padding:.4em;box-sizing:border-box}"
      "button{font-size:1em;padding:.6em 1.2em;margin-top:1em}"
      "small{color:#666}</style><h3>Caméra laser — réseau</h3>");

  if (portal_mode) {
    httpd_resp_sendstr_chunk(req,
        "<p>Mode <b>point d'accès</b> : la caméra fonctionne déjà ici "
        "(<a href=/>image</a>). Choisis un réseau pour qu'elle rejoigne "
        "ton WiFi au prochain démarrage.</p>");
  } else {
    httpd_resp_sendstr_chunk(req, "<p>Réseau actuel : <b>");
    httpd_resp_sendstr_chunk(req, html_escape(cfg_ssid).c_str());
    httpd_resp_sendstr_chunk(req, "</b></p>");
  }

  httpd_resp_sendstr_chunk(req,
      "<form method=POST action=/save>"
      "<label>Réseau</label><select name=ssid id=s>");
  // Balayage a la demande : la page se charge en 2-3 s, mais sans JS ni requete
  // supplementaire — et sur un portail captif, moins il y a de pieces mobiles,
  // mieux c'est.
  int n = WiFi.scanNetworks();
  for (int i = 0; i < n && i < 25; i++) {
    String ssid = WiFi.SSID(i);
    if (ssid.isEmpty()) continue;
    String esc = html_escape(ssid);
    httpd_resp_sendstr_chunk(req, "<option value=\"");
    httpd_resp_sendstr_chunk(req, esc.c_str());
    httpd_resp_sendstr_chunk(req, "\"");
    if (ssid == cfg_ssid) httpd_resp_sendstr_chunk(req, " selected");
    httpd_resp_sendstr_chunk(req, ">");
    httpd_resp_sendstr_chunk(req, esc.c_str());
    httpd_resp_sendstr_chunk(req, (" (" + String(WiFi.RSSI(i)) + " dBm)").c_str());
    httpd_resp_sendstr_chunk(req, "</option>");
  }
  WiFi.scanDelete();
  if (n <= 0) httpd_resp_sendstr_chunk(req, "<option value=\"\">(aucun réseau vu)</option>");

  httpd_resp_sendstr_chunk(req,
      "</select>"
      "<label>Réseau masqué (sinon laisser vide)</label>"
      "<input name=ssid2 autocomplete=off>"
      "<label>Mot de passe</label>"
      "<input name=pass type=password autocomplete=off>"
      "<label>Nom de la caméra (mDNS)</label><input name=host value=\"");
  httpd_resp_sendstr_chunk(req, html_escape(cfg_host).c_str());
  httpd_resp_sendstr_chunk(req,
      "\"><small>Elle répondra à ce nom suivi de .local</small>"
      "<button type=submit>Enregistrer et redémarrer</button></form>"
      "<p><a href=/forget>Oublier le réseau</a> — la caméra redémarre en "
      "point d'accès.</p>");
  return httpd_resp_sendstr_chunk(req, NULL);    // fin du corps en morceaux
}

static esp_err_t save_handler(httpd_req_t *req) {
  // POST, pas GET : un mot de passe n'a rien a faire dans une URL, qui se
  // retrouve dans l'historique du navigateur et les journaux.
  char buf[320];
  int total = req->content_len;
  if (total <= 0 || total >= (int)sizeof(buf)) {
    httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "formulaire illisible");
    return ESP_FAIL;
  }
  int got = 0;
  while (got < total) {
    int r = httpd_req_recv(req, buf + got, total - got);
    if (r <= 0) {
      httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "formulaire tronque");
      return ESP_FAIL;
    }
    got += r;
  }
  buf[got] = 0;

  String body(buf);
  String ssid = form_value(body, "ssid2");       // reseau masque : il prime
  if (ssid.isEmpty()) ssid = form_value(body, "ssid");
  String pass = form_value(body, "pass");
  String host = form_value(body, "host");
  if (ssid.isEmpty()) {
    httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "aucun reseau choisi");
    return ESP_FAIL;
  }
  cfg_save(ssid, pass, host);
  Serial.printf("reseau enregistre : %s (redemarrage)\n", ssid.c_str());

  httpd_resp_set_type(req, "text/html");
  String page = "<!doctype html><meta charset=utf-8>"
                "<meta name=viewport content='width=device-width,initial-scale=1'>"
                "<body style=\"font:15px sans-serif;margin:1em\">"
                "<h3>Enregistré</h3><p>La caméra redémarre et rejoint <b>"
                + html_escape(ssid) + "</b>.</p><p>Elle répondra ensuite à "
                "<b>" + html_escape(host.isEmpty() ? cfg_host : host)
                + ".local</b>. Si elle n'y arrive pas, elle rouvre ce point "
                "d'accès.</p>";
  esp_err_t res = httpd_resp_send(req, page.c_str(), HTTPD_RESP_USE_STRLEN);
  // On redemarre depuis loop(), pas ici : il faut que la reponse soit
  // reellement partie avant de couper le WiFi.
  reboot_at = millis() + 1500;
  return res;
}

static esp_err_t forget_handler(httpd_req_t *req) {
  cfg_forget();
  Serial.println("reseau oublie (redemarrage sur le portail)");
  httpd_resp_set_type(req, "text/html");
  esp_err_t res = httpd_resp_send(req,
      "<!doctype html><meta charset=utf-8>"
      "<body style=\"font:15px sans-serif;margin:1em\">"
      "<h3>Oublié</h3><p>La caméra redémarre sur son point d'accès "
      "<b>lasercam-setup</b>.</p>", HTTPD_RESP_USE_STRLEN);
  reboot_at = millis() + 1500;
  return res;
}

/* Attrape-tout du portail captif.
 *
 * Le DNS renvoie notre adresse pour n'importe quel nom ; le systeme du client
 * interroge alors son URL de test habituelle (`/generate_204` sur Android,
 * `/hotspot-detect.html` sur iOS, `/connecttest.txt` sur Windows). On redirige
 * tout vers la page de configuration : c'est ce qui la fait s'ouvrir seule. */
static esp_err_t portal_catch_handler(httpd_req_t *req) {
  httpd_resp_set_status(req, "302 Found");
  String url = "http://" + WiFi.softAPIP().toString() + "/wifi";
  httpd_resp_set_hdr(req, "Location", url.c_str());
  return httpd_resp_send(req, NULL, 0);
}

// --------------------------------------------------------------------------- //
static void servers_start() {
  httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
  cfg.server_port = 80;
  cfg.ctrl_port   = 32768;
  cfg.max_uri_handlers = 12;
  cfg.lru_purge_enable = true;
  // Le comparateur joker sert a l'attrape-tout du portail. Il gere aussi les
  // correspondances exactes, donc les routes precises ci-dessous restent
  // prioritaires : httpd les essaie dans l'ordre d'enregistrement.
  cfg.uri_match_fn = httpd_uri_match_wildcard;

  httpd_uri_t routes[] = {
      {"/",        HTTP_GET,  index_handler,      NULL},
      {"/status",  HTTP_GET,  status_handler,     NULL},
      {"/capture", HTTP_GET,  capture_handler,    NULL},
      {"/control", HTTP_GET,  control_handler,    NULL},
      {"/laser",   HTTP_GET,  laser_handler,      NULL},
      {"/led",     HTTP_GET,  led_handler,        NULL},
      {"/wifi",    HTTP_GET,  wifi_page_handler,  NULL},
      {"/save",    HTTP_POST, save_handler,       NULL},
      {"/forget",  HTTP_GET,  forget_handler,     NULL},
  };

  if (httpd_start(&control_httpd, &cfg) == ESP_OK) {
    for (auto &r : routes) httpd_register_uri_handler(control_httpd, &r);
    if (portal_mode) {
      // ENREGISTRE EN DERNIER : il attraperait sinon les routes ci-dessus.
      httpd_uri_t any = {"/*", HTTP_GET, portal_catch_handler, NULL};
      httpd_register_uri_handler(control_httpd, &any);
    }
  }

  cfg.server_port = 81;
  cfg.ctrl_port   = 32769;
  cfg.max_uri_handlers = 2;
  httpd_uri_t stream_u = {"/stream", HTTP_GET, stream_handler, NULL};
  if (httpd_start(&stream_httpd, &cfg) == ESP_OK) {
    httpd_register_uri_handler(stream_httpd, &stream_u);
  }
}

// --------------------------------------------------------------------------- //
void setup() {
  // La croix laser AVANT tout le reste : un reset ne doit pas allumer un
  // faisceau le temps que le WiFi et la camera demarrent (~2 s).
  pinMode(LASER_PIN, OUTPUT);
  digitalWrite(LASER_PIN, LOW);
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcAttach(LED_PIN, 5000, 8);
#else
  ledcSetup(LED_CHANNEL, 5000, 8);
  ledcAttachPin(LED_PIN, LED_CHANNEL);
#endif
  led_write(0);

  Serial.begin(115200);
  Serial.setDebugOutput(false);
  Serial.printf("\nlasercam v%d\n", FW_VERSION);

  if (!camera_start()) {
    Serial.println("camera absente : on continue, /status repondra quand meme");
  }
  cfg_load();
  // Le mode se decide ICI, une fois pour toutes : si le reseau memorise ne
  // repond pas, la carte ouvre son portail au lieu de disparaitre.
  if (!sta_connect()) portal_start();
  servers_start();
  Serial.println(portal_mode ? "pret (point d'acces)" : "pret");
}

void loop() {
  if (portal_mode) dns.processNextRequest();
  laser_watchdog();
  if (reboot_at && (int32_t)(millis() - reboot_at) >= 0) {
    Serial.println("redemarrage");
    delay(50);
    ESP.restart();
  }
  if (!portal_mode && WiFi.status() != WL_CONNECTED) {
    // Reconnexion silencieuse : la tete bouge, le signal varie, et une coupure
    // ne doit pas demander un reset manuel sur une machine en place. On ne
    // bascule PAS sur le portail ici — ce serait couper la liaison en pleine
    // gravure. Le portail est le choix du demarrage.
    WiFi.reconnect();
    delay(2000);
  }
  delay(portal_mode ? 10 : 100);        // le DNS du portail doit rester vif
}
