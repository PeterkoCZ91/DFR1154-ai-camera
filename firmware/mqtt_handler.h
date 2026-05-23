#ifndef MQTT_HANDLER_H
#define MQTT_HANDLER_H

#include <Arduino.h>

// Set true when HA button "Snapshot" is pressed — consumed by main loop
extern volatile bool mqtt_snapshot_flag;

void initMQTT();
void mqttLoop();
void mqttHandleCommands();   // Call from main loop to execute queued MQTT commands
void mqttPublishMotion(bool detected);
void mqttPublishPerson(bool detected, float confidence);
bool mqttPersonUncertainConnected();
void mqttPublishPersonUncertain(float confidence);
void mqttPublishMotionScore(int score, float percent);
void mqttPublishBrightness(uint8_t brightness);
void mqttPublishStatus();

#endif // MQTT_HANDLER_H
