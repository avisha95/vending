#include <Arduino.h>

const uint8_t SLOT_COUNT = 6;
const unsigned long DISPENSE_DURATION_MS = 5000;
const unsigned long READY_INTERVAL_MS = 10000;
const bool RELAY_ACTIVE_LOW = true;

#if defined(ARDUINO_ARCH_ESP32)
const uint8_t RELAY_PINS[SLOT_COUNT] = {4, 16, 17, 5, 18, 19};
#else
const uint8_t RELAY_PINS[SLOT_COUNT] = {13, 12, 11, 9, 8, 7};
#endif

const uint8_t RELAY_ON = RELAY_ACTIVE_LOW ? LOW : HIGH;
const uint8_t RELAY_OFF = RELAY_ACTIVE_LOW ? HIGH : LOW;

void allRelaysOff() {
  for (uint8_t index = 0; index < SLOT_COUNT; index++) {
    digitalWrite(RELAY_PINS[index], RELAY_OFF);
  }
}

void setup() {
  Serial.begin(9600);
  Serial.setTimeout(100);

  for (uint8_t index = 0; index < SLOT_COUNT; index++) {
    digitalWrite(RELAY_PINS[index], RELAY_OFF);
    pinMode(RELAY_PINS[index], OUTPUT);
  }
  allRelaysOff();
  Serial.println("READY");
}

void loop() {
  if (!Serial.available()) {
    return;
  }

  String command = Serial.readStringUntil('\n');
  command.trim();

  if (command == "PING") {
    Serial.println("PONG");
    return;
  }

  if (!command.startsWith("DISPENSE,")) {
    Serial.println("ERROR,UNKNOWN_COMMAND");
    return;
  }

  int slot = command.substring(9).toInt();
  if (slot < 1 || slot > SLOT_COUNT) {
    Serial.println("ERROR,INVALID_SLOT");
    return;
  }

  uint8_t relayPin = RELAY_PINS[slot - 1];
  digitalWrite(relayPin, RELAY_ON);
  delay(DISPENSE_DURATION_MS);
  digitalWrite(relayPin, RELAY_OFF);

  Serial.print("DONE,");
  Serial.println(slot);

  static unsigned long lastReady = 0;
  if (millis() - lastReady >= READY_INTERVAL_MS) {
    Serial.println("READY");
    lastReady = millis();
  }
}
