#ifndef CAMERA_PINS_H
#define CAMERA_PINS_H

#if defined(CAMERA_MODEL_DFRobot_FireBeetle2_ESP32S3)
#define PWDN_GPIO_NUM  -1
#define RESET_GPIO_NUM -1
#define XCLK_GPIO_NUM  5
#define SIOD_GPIO_NUM  8
#define SIOC_GPIO_NUM  9

#define Y9_GPIO_NUM    4
#define Y8_GPIO_NUM    6
#define Y7_GPIO_NUM    7
#define Y6_GPIO_NUM    14
#define Y5_GPIO_NUM    17
#define Y4_GPIO_NUM    21
#define Y3_GPIO_NUM    18
#define Y2_GPIO_NUM    16
#define VSYNC_GPIO_NUM 1
#define HREF_GPIO_NUM  2
#define PCLK_GPIO_NUM  15

// IR Night Vision
#define IR_LED_PIN     GPIO_NUM_47

#else
#error "Camera model not selected"
#endif

#endif  // CAMERA_PINS_H
