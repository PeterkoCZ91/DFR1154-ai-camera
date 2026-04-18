#ifndef MQTT_HANDLER_H
#define MQTT_HANDLER_H

#include <Arduino.h>

void initMQTT();
void mqttLoop();
void mqttPublishMotion(bool detected);
void mqttPublishPerson(bool detected, float confidence);
void mqttPublishMotionScore(int score, float percent);
void mqttPublishBrightness(uint8_t brightness);
void mqttPublishStatus();

#endif // MQTT_HANDLER_H
