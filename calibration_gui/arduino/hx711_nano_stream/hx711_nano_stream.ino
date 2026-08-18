#include <HX711.h>
#include <string.h>

// Editable wiring and serial constants.
const byte HX711_DOUT_PIN = 4;
const byte HX711_SCK_PIN = 5;
const unsigned long SERIAL_BAUD = 115200;

const char PROTOCOL_VERSION[] = "1.0";
const char FIRMWARE_VERSION[] = "1.0.0";
const unsigned long HX711_READY_TIMEOUT_MS = 1000;
const byte COMMAND_BUFFER_SIZE = 24;

HX711 scale;

// Stream immediately after reset. START/STOP remain optional diagnostics and
// compatibility controls; the host never needs HELLO to prove sensor health.
bool streamingEnabled = true;
unsigned long sampleId = 0;
unsigned long lastFreshConversionMs = 0;
unsigned long lastTimeoutReportMs = 0;
char commandBuffer[COMMAND_BUFFER_SIZE];
byte commandLength = 0;

void emitHello() {
  Serial.print(F("HELLO,HX711_NANO,"));
  Serial.print(PROTOCOL_VERSION);
  Serial.print(',');
  Serial.println(FIRMWARE_VERSION);
}

void emitStatus() {
  // HELLO is optional device information. Valid DATA rows are the measurement
  // evidence used by the GUI to validate the sensor.
  emitHello();
  Serial.print(F("STATUS,"));
  Serial.print(streamingEnabled ? F("STREAMING") : F("STOPPED"));
  Serial.print(',');
  Serial.print(scale.is_ready() ? F("READY") : F("NOT_READY"));
  Serial.print(',');
  Serial.println(sampleId);
}

void handleCommand(const char *command) {
  if (strcmp(command, "PING") == 0) {
    Serial.println(F("PONG,HX711_NANO"));
    return;
  }

  if (strcmp(command, "START") == 0) {
    streamingEnabled = true;
    lastFreshConversionMs = millis();
    lastTimeoutReportMs = lastFreshConversionMs;
    Serial.println(F("OK,START"));
    if (!scale.is_ready()) {
      Serial.println(F("ERROR,HX711_NOT_READY"));
    }
    return;
  }

  if (strcmp(command, "STOP") == 0) {
    streamingEnabled = false;
    Serial.println(F("OK,STOP"));
    return;
  }

  if (strcmp(command, "STATUS") == 0) {
    emitStatus();
    return;
  }

  Serial.print(F("ERROR,UNKNOWN_COMMAND,"));
  Serial.println(command);
}

void pollSerialCommands() {
  // Consume only bytes already available; never wait for a line to arrive.
  while (Serial.available() > 0) {
    const char incoming = static_cast<char>(Serial.read());

    if (incoming == '\n' || incoming == '\r') {
      if (commandLength > 0) {
        commandBuffer[commandLength] = '\0';
        handleCommand(commandBuffer);
        commandLength = 0;
      }
      continue;
    }

    if (commandLength < COMMAND_BUFFER_SIZE - 1) {
      commandBuffer[commandLength++] = incoming;
    } else {
      commandLength = 0;
      Serial.println(F("ERROR,COMMAND_TOO_LONG"));
    }
  }
}

void serviceHx711() {
  if (!streamingEnabled) {
    return;
  }

  if (scale.is_ready()) {
    // read() consumes this completed conversion. It is never called unless the
    // HX711 reports ready, so stale cached values are never re-emitted.
    const long rawAdc = scale.read();
    const unsigned long sampleMicros = micros();

    Serial.print(F("DATA,"));
    Serial.print(sampleId);
    Serial.print(',');
    Serial.print(sampleMicros);
    Serial.print(',');
    Serial.println(rawAdc);

    ++sampleId;
    lastFreshConversionMs = millis();
    return;
  }

  const unsigned long nowMs = millis();
  if (nowMs - lastFreshConversionMs >= HX711_READY_TIMEOUT_MS &&
      nowMs - lastTimeoutReportMs >= HX711_READY_TIMEOUT_MS) {
    Serial.println(F("ERROR,HX711_TIMEOUT"));
    lastTimeoutReportMs = nowMs;
  }
}

void setup() {
  Serial.begin(SERIAL_BAUD);
  scale.begin(HX711_DOUT_PIN, HX711_SCK_PIN);
  lastFreshConversionMs = millis();
  lastTimeoutReportMs = lastFreshConversionMs;
  emitHello();
  if (!scale.is_ready()) {
    Serial.println(F("ERROR,HX711_NOT_READY"));
  }
}

void loop() {
  pollSerialCommands();
  serviceHx711();
}
