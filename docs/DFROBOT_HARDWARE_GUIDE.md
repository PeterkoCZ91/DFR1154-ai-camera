# DFRobot FireBeetle 2 ESP32-S3 (AI Camera) - Hardware Guide

Tento dokument shrnuje hardwarové schopnosti a mapování pinů pro desku DFRobot ESP32-S3 AI Camera, zjištěné z analýzy oficiálních příkladů (DFR1154_Examples).

## 1. Audio Systém

### Mikrofon (Integrovaný)
*   **Typ:** PDM (Pulse Density Modulation)
*   **Rozhraní:** I2S (RX)
*   **Piny:**
    *   **CLK:** GPIO **38**
    *   **DATA:** GPIO **39**
*   **Poznámka:** Pro správnou funkci na ESP32-S3 vyžaduje buď Arduino Core 3.x (nový driver) nebo specifické nastavení na Core 2.x.

### Reproduktor / Zesilovač (Integrovaný)
*   **Čip:** MAX98357 (I2S Amplifier)
*   **Rozhraní:** I2S (TX)
*   **Piny:**
    *   **BCLK:** GPIO **46**
    *   **LRC (WS):** GPIO **45**
    *   **DIN (Data In):** GPIO **42**
*   **Využití:** Přehrávání zvuků, poplachů, TTS (Text-to-Speech), obousměrné audio.

## 2. Senzory a Periferie

### Senzor okolního světla (Ambient Light Sensor)
*   **Čip:** LTR-308
*   **Rozhraní:** I2C
*   **Adresa:** (Standardní I2C adresa LTR-308)
*   **Piny:** Používá výchozí I2C piny desky (pravděpodobně SDA=1, SCL=2 nebo SDA=8, SCL=9 - nutno ověřit v `pins_arduino.h` pro tuto desku).
*   **Využití:** Automatické přepínání nočního režimu (IR Cut / IR LED) na základě reálného osvětlení.
*   **Knihovna:** `DFRobot_LTR308`

### IR Přísvit (Night Vision)
*   **IR LED:** Ovládání pravděpodobně přes GPIO (nutno dohledat, často sdíleno s kamerou nebo samostatný pin). V našem kódu používáme `IR_LEDS_PIN` (často GPIO 2 nebo 48).
*   **IR Cut Filter:** Mechanický filtr, který se přepíná.

### Indikační LED
*   **Pin:** GPIO **3**
*   **Využití:** Signalizace stavu (Boot, WiFi, Recording).

### Tlačítka
*   **Boot Button:** GPIO **0**
*   **Reset Button:** Hardwarový reset.

## 3. Kamera
*   **Model:** OV3660 (nebo OV2640)
*   **Rozhraní:** DVP (Parallel)
*   **Piny (Standardní ESP32-S3 Camera):**
    *   XCLK: 10
    *   PCLK: 12
    *   VSYNC: 13
    *   HREF: 14
    *   D0-D7: 35, 36, 37, 34, 33, 48, 47, 21 (Příklad, liší se dle verze).
    *   SIOD: 5
    *   SIOC: 6
    *   PWDN: -1 (často)
    *   RESET: -1 (často)

## 4. Další funkce
*   **USB Webkamera:** Deska může fungovat jako USB UVC zařízení (příklad `5.4 USBWebCamera`).
*   **SD Karta:** Integrovaný slot (SDMMC nebo SPI).

## 5. Doporučení pro vývoj (A12 System)

1.  **Audio:** Priorita je zprovoznit mikrofon (piny 38/39). Pokud to nepůjde na Core 2.x, zvážit přechod na Core 3.x s vyřešením boot loopu.
2.  **Automatizace:** Implementovat čtení z LTR-308 pro inteligentní řízení nočního režimu.
3.  **Interakce:** Využít reproduktor pro zvukovou zpětnou vazbu (např. "pípnutí" při detekci obličeje).
