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

// Broker-link observability for /status. A camera without a serial console had
// no way to show that the link was flapping, which is what kept the reconnect
// crash fixed in 3.12.51 invisible for 115 days. mqtt_connects > 1 means the
// link has dropped and re-established at least once since boot.
bool mqttLinkUp();
int mqttLastState();          // last PubSubClient rc: 0 ok, 5 unauthorized, -4 timeout
uint32_t mqttConnectCount();  // successful connects since boot
uint32_t mqttFailCount();     // failed connect attempts since boot
uint32_t mqttLinkUptimeSeconds();  // 0 when the link is down

#endif // MQTT_HANDLER_H
