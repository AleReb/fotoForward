/*
 * ============================================================================
 * Unified_SIM7600_SD_Image_Manager.ino
 * ============================================================================
 * Proyecto:     Captura, recepción, almacenamiento y envío de imágenes con ESP32 y SIM7600
 * Autor:        Alejandro Rebolledo
 * Fecha:        2025-05-18
 *
 * Descripción general:
 * Este sketch para ESP32 permite gestionar un flujo completo de imágenes entre
 * una Raspberry Pi y un servidor HTTP usando el módem SIM7600. El sistema funciona así:
 *
 * 1. ESP32 espera un encabezado vía UART2 desde Raspberry Pi (Serial2), indicando el nombre 
 *    de archivo y tamaño de la imagen a recibir.
 * 2. Recibe los datos binarios de la imagen por bloques (chunks) y los guarda en la tarjeta SD.
 * 3. Luego de guardar exitosamente la imagen, se envía automáticamente al servidor remoto
 *    vía HTTP POST a través del SIM7600.
 * 4. Incluye funcionalidades para:
 *      - Conexión y diagnóstico de red móvil (APN, CSQ, CREG, COPS)
 *      - Actualización del reloj RTC desde un servidor
 *      - Visualización de estado de red en pantalla OLED
 *      - Peticiones automáticas o manuales de imagen a Raspberry Pi
 *
 * Interfaz serial:
 * - USB Serial:
 *     "s" → Envía la última imagen recibida al servidor (POST).
 *     "p" → Solicita una nueva foto a la Raspberry Pi por UART.
 *     "t" → Actualiza la hora desde un servidor remoto.
 *     "C" → Solicita y envía automáticamente una imagen (modo completo).
 * - UART2 (Serial2):
 *     Espera el comando “foto” desde Raspberry Pi → Inicia secuencia de recepción.
 *
 * Handshake UART entre ESP32 y Raspberry Pi:
 *     Raspberry Pi → "nombre.jpg|tamaño_en_bytes\n"
 *     ESP32 → "READY"
 *     (bloques de datos binarios JPEG)
 *     ESP32 → "ACK" por cada bloque
 *     ESP32 → "DONE" al finalizar
 *
 * Requisitos:
 * - Módem SIM7600 conectado vía UART (Serial1)
 * - Tarjeta SD (SPI) con CS en pin 4
 * - Pantalla OLED 128x128 con driver SH1107 (I2C)
 * - RTC DS3231
 *
 * Bibliotecas requeridas:
 * - TinyGSM
 * - ArduinoJson
 * - U8g2lib
 * - RTClib
 * - SD
 *
 * ============================================================================
 */


#define TINY_GSM_MODEM_SIM7600
#define TINY_GSM_RX_BUFFER 2014  // Set RX buffer to 2Kb
#define SerialAT Serial1

#include <Arduino.h>
#include <StreamDebugger.h>
#include <TinyGsmClient.h>
#include <U8g2lib.h>
#include <Wire.h>
#include <RTClib.h>
#include <ArduinoJson.h>
#include "FS.h"
#include "SD.h"
#include "SPI.h"
#define SD_CS_PIN 4

// --- Global variables for image file --------------------
// --- Transfer settings ---------------------------------------------------
#define CHUNK_SIZE 1024
int RECEIVE_TIMEOUT = 45000;  // este timeoyt va a ser nas grande cuando lo intente por segunda vez solo para preubas
unsigned long lastByteTime = 0;  // To track last received byte time
bool sendAfterReceive = false;
bool receiving = false;
bool SavedSDafter = false;
File outFile;
String filename;
int fileSize = 0;
int bytesReceived = 0;
String PHOTO_PATH = "";  // Ensure PHOTO_PATH is always initialized
bool hasRetried = false;

//const char *WEBHOOK_URLOLD = "https://webhook.site/422dc1ed-dcb0-4114-9f5a-c73bf9e88423";  //pruebas exitoasas
const char *WEBHOOK_URL = "https://hermit-harmless-wildly.ngrok-free.app/agregarImagen";  //pruebas exitoasas
int ID = 1; //debiese ser el del dispositivo completo
// --- Serial port pins -----------------------------------------------------
#define MODEM_RX_PIN 17
#define MODEM_TX_PIN 16
#define FILE_RX_PIN 26
#define FILE_TX_PIN 27
// Configuración de tiempos
#define uS_TO_S_FACTOR 1000000ULL  // Factor de conversión de microsegundos a segundos
#define TIME_TO_SLEEP 30           // Tiempo de sleep en segundos
#define UART_BAUD 115200

// --- HardwareSerial instances --------------------------------------------

StreamDebugger debugger(SerialAT, Serial);
TinyGsm modem(debugger);
TinyGsmClient client(modem);
//TinyGsmClientSecure client(modem);
HardwareSerial fileSerial(2);  // Serial2 for image transfer

// --- Display & RTC --------------------------------------------------------
U8G2_SH1107_SEEED_128X128_F_HW_I2C display(U8G2_R0);
RTC_DS3231 rtc;

// --- GPRS & Webhook config -----------------------------------------------
const char *APN = "gigsky-02";
const char *GPRS_USER = "";
const char *GPRS_PASS = "";
String networkOperator;
String networkTech;
String signalQuality;
String registrationStatus;
String httpReadData;
String lastPostedID;

// --- State variables ------------------------------------------------------
const unsigned long networkInterval = 60000 * 3;  // 45 s
const unsigned long sendInterval = 60000 * 60;    // 100 min 6000000
unsigned long lastNetworkUpdate = 0;
unsigned long lastDataSend = 0;

// --- Setup ---------------------------------------------------------------
void setup() {
  Serial.begin(UART_BAUD);
  setupHardware();
  testSIM();
  if (!connectToNetwork()) {
    Serial.println("Network connect failed");
  }
  updateNetworkInfo();
  lastNetworkUpdate = millis();
  lastDataSend = millis();
}

void loop() {
  // 1) Si NO estamos recibiendo Y llegan bytes por fileSerial, leo encabezado
  if (!receiving && fileSerial.available()) {
    readHeader();
  }
  // 2) Si estamos recibiendo, proceso la recepción y salgo
  else if (receiving) {
    processReception();
    return;
  }
  // 3) Si no estamos recibiendo y NO hay encabezado nuevo, ejecuto tareas normales
  else {
    if (sendAfterReceive && SavedSDafter) {
      Serial.println("Sending image after receive...");
      sendImageWebhook();
      sendAfterReceive = false;
      SavedSDafter = false;
    }
    readModemResponses();
    handleSerialCommands();  // <- aquí vuelven tus comandos “s”, “p”, “C”, etc.
    loopNormalTasks();
  }
}
// --- Lee y parsea el encabezado "filename|size\n" ---
void readHeader() {
  filename = "";
  Serial.print("Receiving header: ");
  // Leo hasta '\n'
  while (fileSerial.available()) {
    char c = fileSerial.read();
    if (c == '\n') break;
    filename += c;
  }

  int sep = filename.indexOf('|');
  if (sep < 0) {
    Serial.println("  Invalid header: " + filename);
    return;
  }

  String nameOnly = filename.substring(0, sep);
  fileSize = filename.substring(sep + 1).toInt();
  Serial.printf("  File: %s  Size: %u bytes\n", nameOnly.c_str(), fileSize);
  // Construyo ruta única y abro SD
  int randId = random(1, 5);//este random es para pruebas sacar para version final
  PHOTO_PATH = "/" + String(randId) + "_" + nameOnly + ".jpg";
  outFile = SD.open(PHOTO_PATH, FILE_WRITE);
  if (!outFile) {
    Serial.println("  ERROR: could not open " + PHOTO_PATH);
    return;
  }

  // Preparo variables de recepción
  bytesReceived = 0;
  hasRetried   = false;    // reinicio antes de empezar a recibir
  lastByteTime = millis();
  receiving = true;
  Serial.println("  Start receiving data...");
  fileSerial.println("READY");  // <--- aviso de inicio
}

void processReception() {
  // Mientras falten bytes…
  while (bytesReceived < fileSize) {
    if (fileSerial.available()) {
      size_t toRead = min((size_t)fileSize - bytesReceived, (size_t)CHUNK_SIZE);
      uint8_t buf[CHUNK_SIZE];
      size_t n = fileSerial.read(buf, toRead);
      if (n > 0) {
        outFile.write(buf, n);
        bytesReceived += n;
        lastByteTime = millis();
        Serial.print(".");
        fileSerial.println("ACK");
      }
    }

    // Timeout => intentamos un único reintento
    if (millis() - lastByteTime > RECEIVE_TIMEOUT) {
      outFile.close();
      Serial.printf("\nTIMEOUT: %u/%u bytes\n", bytesReceived, fileSize);
      fileSerial.println("NACK_TIMEOUT");
      receiving = false;
      if (!hasRetried) {
        hasRetried = true;
        delay(3000);
        Serial.println("Retrying reception once...");
        fileSerial.println("foto");   // pedimos la retransmisión
      }
      return;
    }
  }

  // Ya recibimos todo
  outFile.close();
  Serial.println("\nFile saved: " + PHOTO_PATH);
  fileSerial.println("DONE");
  receiving    = false;
  SavedSDafter = true;
  // NO reiniciamos hasRetried aquí; el próximo header hará reset.
}

// --- Hardware initialization --------------------------------------------
void setupHardware() {
  // USB-Serial and AT-Serial
  SerialAT.begin(UART_BAUD, SERIAL_8N1, MODEM_RX_PIN, MODEM_TX_PIN);
  modem.restart();
  modem.init();
  // File-transfer serial
  fileSerial.begin(UART_BAUD, SERIAL_8N1, FILE_RX_PIN, FILE_TX_PIN);

  // OLED
  display.begin();
  display.clearBuffer();
  display.setFont(u8g2_font_ncenB08_tr);
  display.drawStr(0, 24, "INIT");
  display.sendBuffer();

  // RTC
  if (!rtc.begin()) {
    Serial.println("RTC init failed");
    display.clearBuffer();
    display.drawStr(0, 24, "RTC FAIL");
    display.sendBuffer();
  }
  SPI.begin();  // <— ensures SCK/MISO/MOSI are driven
  // SD card
  if (!SD.begin(SD_CS_PIN)) {
    Serial.println("SD init failed");
    display.clearBuffer();
    display.drawStr(0, 24, "SD FAIL");
    display.sendBuffer();
    delay(1000);
  }
}

// --- Handle commands from USB serial ------------------------------------
void handleSerialCommands() {
  if (!Serial.available()) return;
  String cmd = Serial.readStringUntil('\n');
  cmd.trim();
  if (cmd.equalsIgnoreCase("s")) {
    sendImageWebhook();  //directo tipo foto
                         // sendImageMultipart();//nuevo cmas
  } else if (cmd.equalsIgnoreCase("p")) {
    fileSerial.println("foto");
    Serial.println("Requested foto");
  } else if (cmd.equalsIgnoreCase("t")) {
    rtcUpdate();
  } else if (cmd.equalsIgnoreCase("C")) {
    Serial.println("TEST Auto request photo");

    fileSerial.println("foto");
    sendAfterReceive = true;  // envío tras recibir

  } else if (cmd.equalsIgnoreCase("r")) {
    //sendImageMultipartResilient();  //nuevo cmas sin esperar errores
  }
}

// --- Send image via SIM7600 HTTP POST -----------------------------------
void sendImageWebhook() {
  // 1) Abrir el archivo que se acaba de guardar
  //nueva forma para testear el guardar varias fotos en archivos segun id
  // 1. Extraer el nombre del archivo de PHOTO_PATH
  String filename = "";
  int lastSlashIndex = PHOTO_PATH.lastIndexOf('/');
  if (lastSlashIndex != -1) {
    filename = PHOTO_PATH.substring(lastSlashIndex + 1);
  } else {
    filename = PHOTO_PATH;
  }

  // 2. Extraer el sensorId del 'filename'
  String sensorId = "";
  int underscoreIndex = filename.indexOf('_');
  if (underscoreIndex != -1) {
    sensorId = filename.substring(0, underscoreIndex);
  } else {
    // Manejo de error si el nombre del archivo no tiene el formato esperado
    Serial.println("Error: El nombre del archivo no contiene un ID valido (ej. ID_timestamp.jpg)");
    return;  // O maneja el error de otra manera, quizás asignando un ID por defecto
  }

  Serial.println("Filename extraído: " + filename);
  Serial.println("Sensor ID extraído: " + sensorId);

  String fullUrl = String(WEBHOOK_URL) + "?id_sensor=" + String(sensorId) + "&filename=" + filename;

  Serial.println("OPEN FILE: " + PHOTO_PATH);
  File img = SD.open(PHOTO_PATH, FILE_READ);  //originalmente
  if (!img) {
    Serial.println("Image open failed");
    return;
  }
  size_t imgSize = img.size();
  Serial.printf("Image size: %u bytes\n", imgSize);
  // 2) Cerrar cualquier sesión HTTP previa y arrancar nuevo módulo HTTP
  modem.sendAT("+HTTPTERM");
  modem.waitResponse(2000);

  modem.sendAT("+HTTPINIT");
  if (modem.waitResponse(5000) != 1) {
    Serial.println("HTTPINIT failed");
    img.close();
    return;
  }

  // 3) Setear parámetros HTTP básicos
  modem.sendAT("+HTTPPARA=\"CID\",1");
  modem.waitResponse(2000);
  modem.sendAT(String("+HTTPPARA=\"URL\",\"") + fullUrl + "\"");  //fullUrl  modem.sendAT(String("+HTTPPARA=\"URL\",\"") + WEBHOOK_URLOLD + "\"");
  modem.waitResponse(2000);
  modem.sendAT("+HTTPPARA=\"CONTENT\",\"image/jpeg\"");
  modem.waitResponse(2000);

  // 4) Informar el tamaño que vamos a enviar y esperar prompt “>”
  modem.sendAT(String("+HTTPDATA=") + imgSize + ",10000");
  // Esperamos a “DOWNLOAD” o a “>” para empezar a subir bytes
  if (modem.waitResponse(8000, ">") != 1 && modem.waitResponse(8000, "DOWNLOAD") != 1) {
    Serial.println("No HTTPDATA prompt, continuing anyway");
  }

  // 5) Stream de bytes de la imagen
  uint8_t buf[256];
  while (img.available()) {
    size_t n = img.read(buf, sizeof(buf));
    modem.stream.write(buf, n);
    delay(5);
  }
  img.close();
  Serial.println("Image data sent");

  // 6) Esperar confirmación de OK
  modem.waitResponse(10000);

  // 7) Lanzar la acción POST
  modem.sendAT("+HTTPACTION=1");
  // La respuesta +HTTPACTION: será procesada en readModemResponses()
}
// --- Process asynchronous modem events ----------------------------------
void readModemResponses() {
  while (modem.stream.available()) {
    String line = modem.stream.readStringUntil('\n');
    line.trim();

    if (line.startsWith("+HTTPACTION:")) {
      // parse status and length
      int c1 = line.indexOf(','), c2 = line.indexOf(',', c1 + 1);
      int status = line.substring(c1 + 1, c2).toInt();
      int length = line.substring(c2 + 1).toInt();
      Serial.printf("HTTPACTION status=%d length=%d\n", status, length);

      if (status == 200 && length > 0) {
        modem.sendAT(String("+HTTPREAD=0,") + length);
      }
    } else if (line.startsWith("+HTTPREAD:")) {
      int comma = line.indexOf(',');
      int length = (comma > 0) ? line.substring(comma + 1).toInt() : 0;
      httpReadData = "";
      unsigned long t0 = millis();
      while (millis() - t0 < 12000 && httpReadData.length() < length) {
        if (modem.stream.available())
          httpReadData += (char)modem.stream.read();
      }
      Serial.println("HTTPREAD data: " + httpReadData);
      // parse JSON or timestamp as before...
    } else if (line == "OK") {
      closeHttpSession();
    }
  }
}

// --- Execute AT command with display -------------------------------------
void executeATCommand(const String &cmd, unsigned long timeout) {
  while (modem.stream.available()) modem.stream.read();
  Serial.println("AT> " + cmd);
  displayModemResponse(cmd, "");
  modem.sendAT(cmd);
  String resp;
  modem.waitResponse(timeout, resp);
  Serial.println("AT< " + resp);
  displayModemResponse(cmd, resp);
}

// --- Display modem cmd/result on OLED -----------------------------------
void displayModemResponse(const String &cmd, const String &resp) {
  display.clearBuffer();
  display.setFont(u8g2_font_ncenB08_tr);
  display.setCursor(0, 12);
  display.print("->");
  display.print(cmd);
  display.setCursor(0, 24);
  display.print("<-");
  display.print(resp);
  display.sendBuffer();
}

// --- Basic SIM test ------------------------------------------------------
void testSIM() {
  executeATCommand("AT", 2000);
  executeATCommand("AT+CPIN?", 2000);
  executeATCommand("AT+CREG?", 2000);
  executeATCommand("AT+CGPADDR", 2000);
}

// --- Connect to network & GPRS -------------------------------------------
bool connectToNetwork() {
  for (int i = 0; i < 3; ++i) {
    Serial.println("Waiting for network...");
    if (modem.waitForNetwork() && modem.gprsConnect(APN, GPRS_USER, GPRS_PASS)) {
      Serial.println("GPRS connected");
      return true;
    }
    delay(5000);
  }
  return false;
}

// --- Fallback network info via AT ----------------------------------------
// Obtiene: Operador, Tecnología, CSQ y Registro.
void getNetworkInfoFallback(String &opOut, String &techOut, String &csqOut, String &regOut) {
  Serial.println("Ejecutando fallback para info de red...");

  // Enviar AT+COPS? para operador y tecnología
  SerialAT.println("AT+COPS?");
  delay(100);
  String copsResponse = "";
  while (SerialAT.available()) {
    copsResponse += SerialAT.readStringUntil('\n');
  }
  copsResponse.trim();
  Serial.println("Fallback COPS response: " + copsResponse);

  // Extraer operador (entre comillas)
  int firstQuote = copsResponse.indexOf("\"");
  int secondQuote = copsResponse.indexOf("\"", firstQuote + 1);
  if (firstQuote != -1 && secondQuote != -1) {
    opOut = copsResponse.substring(firstQuote + 1, secondQuote);
  } else {
    opOut = "N/A";
  }

  // Extraer tecnología (último parámetro, se espera dígito)
  int lastComma = copsResponse.lastIndexOf(",");
  String techString = "";
  if (lastComma != -1 && copsResponse.length() > lastComma + 1) {
    String techNumStr = copsResponse.substring(lastComma + 1);
    techNumStr.trim();
    int techNum = techNumStr.toInt();
    switch (techNum) {
      case 0: techString = "GSM"; break;
      case 1: techString = "GSM Compact"; break;
      case 2: techString = "UTRAN (3G)"; break;
      case 3: techString = "EDGE"; break;
      case 4: techString = "HSPA"; break;
      case 5: techString = "HSPA"; break;
      case 6: techString = "HSPA+"; break;
      case 7: techString = "LTE"; break;
      default: techString = "Unknown"; break;
    }
  } else {
    techString = "N/A";
  }
  techOut = techString;

  // Obtener CSQ mediante AT+CSQ
  SerialAT.println("AT+CSQ");
  delay(100);
  String csqResponse = "";
  while (SerialAT.available()) {
    csqResponse += SerialAT.readStringUntil('\n');
  }
  csqResponse.trim();
  // Se espera una respuesta tipo: "+CSQ: xx,yy"
  int colonIndex = csqResponse.indexOf(":");
  if (colonIndex != -1) {
    int commaIndex = csqResponse.indexOf(",", colonIndex);
    if (commaIndex != -1) {
      csqOut = csqResponse.substring(colonIndex + 1, commaIndex);
      csqOut.trim();
    } else {
      csqOut = csqResponse;
    }
  } else {
    csqOut = "N/A";
  }

  // Obtener estado de registro mediante AT+CREG?
  SerialAT.println("AT+CREG?");
  delay(100);
  String cregResponse = "";
  while (SerialAT.available()) {
    cregResponse += SerialAT.readStringUntil('\n');
  }
  cregResponse.trim();
  Serial.println("Fallback CREG response: " + cregResponse);
  if (cregResponse.indexOf("0,1") != -1 || cregResponse.indexOf("0,5") != -1) {
    regOut = "Conectado";
  } else {
    regOut = "No registrado";
  }

  // Mostrar resultados del fallback por Serial
  Serial.println("Fallback Operador: " + opOut);
  Serial.println("Fallback Tecnología: " + techOut);
  Serial.println("Fallback CSQ: " + csqOut);
  Serial.println("Fallback Registro: " + regOut);
}

// --- Update & display network info ---------------------------------------
void updateNetworkInfo() {
  getNetworkInfoFallback(networkOperator, networkTech, signalQuality, registrationStatus);
  display.clearBuffer();
  display.setFont(u8g2_font_ncenB08_tr);
  display.drawStr(0, 16, ("Op: " + networkOperator).c_str());
  display.drawStr(0, 32, ("Tec:" + networkTech).c_str());
  display.drawStr(0, 46, ("CSQ:" + signalQuality).c_str());
  display.drawStr(0, 64, ("Reg:" + registrationStatus).c_str());
  display.sendBuffer();
}

// --- Terminate HTTP session ----------------------------------------------
void closeHttpSession() {
  modem.sendAT("+HTTPTERM");
  modem.waitResponse(2000);
}

// --- Update RTC from server ----------------------------------------------
void rtcUpdate() {
  closeHttpSession();
  modem.sendAT("+HTTPINIT");
  if (modem.waitResponse(5000) != 1) return;
  modem.sendAT("+HTTPPARA=\"URL\",\"https://southamerica-west1-fic-aysen-412113.cloudfunctions.net/unixTime\"");
  modem.waitResponse(5000);
  executeATCommand("+HTTPACTION=0", 5000);
  // readModemResponses() will handle timestamp
  closeHttpSession();
}

// --- Periodic tasks & auto-request photo -------------------------------
void loopNormalTasks() {
  unsigned long now = millis();

  // Cada sendInterval milisegundos...
  if (now - lastDataSend >= sendInterval) {
    lastDataSend = now;  //sistema de envio cada 1 hora
    Serial.println("autmata send foto");
    fileSerial.println("foto");
    sendAfterReceive = true;  // envío tras recibir
  }

  // Actualización periódica de red (sin cambios)
  if (now - lastNetworkUpdate >= networkInterval) {
    lastNetworkUpdate = now;
    updateNetworkInfo();
  }
}
