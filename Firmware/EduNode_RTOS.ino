/*
 * =========================================================================================
 * Project: EduNode Autonomous UAV Ground Control Station
 * Target Platform: MYOSA Mini IoT Kit (ESP32)
 * Authors: Lixin Tu & Luis Arenas
 *
 * Firmware V35: Final Release.
 * - FIXED: Scope declaration error for buzzer and override variables.
 * - ACTIVE: OLED Fan Animation, OLED Fan Graph, Edge AI Prediction, 24h Drift Filter.
 * - ACTIVE: Dual-Layer Gyro filtering and Hardware I2C Mutex protection.
 * =========================================================================================
 */

#include <Wire.h>
#include <WiFi.h>
#include <BluetoothSerial.h>
#include <Adafruit_HTU21DF.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <AccelAndGyro.h>
#include <LightProximityAndGesture.h>
#include <Preferences.h> 

// Native ESP32 internal silicon temperature sensor dependency
#ifdef __cplusplus
extern "C" {
uint8_t temprature_sens_read();
}
#endif

// Hardware Pin Definitions
const int MOSFET_PIN = 16;      
const int LED_TEST_PIN = 2;     
const int BUZZER_PIN = 4;       
const int PWM_FREQ = 25000;     
const int PWM_RESOLUTION = 8;   

// OLED Configuration
#define SCREEN_WIDTH 128
#define SCREEN_HEIGHT 64
#define OLED_RESET    -1 
Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, OLED_RESET);

// Sensor Objects
Adafruit_HTU21DF htu = Adafruit_HTU21DF();
AccelAndGyro Ag;
LightProximityAndGesture Lpg;
BluetoothSerial SerialBT;
Preferences nvsConfig; 

// Network Configuration
const char* ssid = "TELUS1825";       
const char* password = "z7n5zg2kck";  
WiFiServer server(80);
String currentIP = "";

// Global System Data Structure
struct SystemData {
  float currentTemp;
  float currentHum;
  float tiltX;
  float tiltY;
  uint16_t ambientLight;
  float rateOfChange;
  int currentFanDuty;
  
  bool tiltWarning;
  bool intrusionWarning; 
  
  int targetTemp; 
  bool manualOverride;
  int manualFanDuty;
  bool buzzerActive;
  bool buzzerMuted; 
  
  bool triggerCalibration; 
  bool gyroAlarmEnabled;   
  
  float cpuTemp;        
  float predictedTemp; 
  float estimatedCurrent; 
} sysData;

// RTOS Semaphores
SemaphoreHandle_t dataMutex;
SemaphoreHandle_t i2cMutex;

// RTOS Tasks
TaskHandle_t Core0Task;
TaskHandle_t Core1Task;

// PD Controller Constants
const float Kp = 15.0;            
const float Kd = 65.0;            

// UI Graph Variables
#define GRAPH_WIDTH 42
int fanHistory[GRAPH_WIDTH] = {0}; 
int animationFrame = 0;
bool toggleWarning = false;

/* ==================================================================
 * CORE 0: SENSORS, EDGE AI & THERMAL CONTROL
 * ================================================================== */
void Core0_SensorsAndControl(void *pvParameters) {
  unsigned long previousPDTime = 0;
  unsigned long previousFastPoll = 0;
  
  float uiX = 0, uiY = 0; 
  float bootZeroX = 0, bootZeroY = 0;
  int consecutiveTilts = 0; 
  int shakeTimer = 0;
  
  bool fanWasOff = true;
  unsigned long kickstartTime = 0;

  uint16_t tempLux = 0;
  float tempC = 0, hum = 0;
  float smoothedTempC = 0.0; // Thermal low-pass filter
  int finalDuty = 0;
  float dT = 0;
  
  // Chebyshev AI polynomial buffer
  float t_n0 = 0, t_n1 = 0, t_n2 = 0; 
  bool aiReady = false;
  float projectedTemp = 0.0;
  
  bool isTilting = false;
  bool isBoxOpen = false;
  int localTarget = 30; 
  bool firstRead = true;
  
  // Load NVS Calibration Data
  nvsConfig.begin("edunode", true);
  bootZeroX = nvsConfig.getFloat("calX", 0.0);
  bootZeroY = nvsConfig.getFloat("calY", 0.0);
  nvsConfig.end();
  
  for (;;) {
    unsigned long currentMillis = millis();
    bool isGyroAlarmEnabled = false;
    bool runCalibration = false;
    
    // Fetch command requests via Mutex
    if (xSemaphoreTake(dataMutex, 0) == pdTRUE) {
      if (sysData.triggerCalibration) { runCalibration = true; sysData.triggerCalibration = false; }
      isGyroAlarmEnabled = sysData.gyroAlarmEnabled;
      localTarget = sysData.targetTemp;
      xSemaphoreGive(dataMutex);
    }

    // Dynamic Gyro Calibration Routine
    if (runCalibration) {
      float sumX = 0, sumY = 0; int validSamples = 0;
      for(int i=0; i<30; i++) {
        xSemaphoreTake(i2cMutex, portMAX_DELAY);
        if (Ag.ping()) { sumX += Ag.getTiltX(); sumY += Ag.getTiltY(); validSamples++; }
        xSemaphoreGive(i2cMutex);
        vTaskDelay(pdTICKS_TO_MS(20)); 
      }
      if (validSamples > 0) {
        bootZeroX = sumX / validSamples; bootZeroY = sumY / validSamples;
        nvsConfig.begin("edunode", false);
        nvsConfig.putFloat("calX", bootZeroX); nvsConfig.putFloat("calY", bootZeroY);
        nvsConfig.end();
        uiX = bootZeroX; uiY = bootZeroY;
      }
    }

    // 1. FAST SENSOR POLLING (200ms)
    if (currentMillis - previousFastPoll >= 200) {
      previousFastPoll = currentMillis;
      bool gyroOk = false; float rawX = 0, rawY = 0;

      // Protected I2C Bus Read
      xSemaphoreTake(i2cMutex, portMAX_DELAY);
      if (Ag.ping()) { gyroOk = true; rawX = Ag.getTiltX(); rawY = Ag.getTiltY(); }
      tempLux = Lpg.getAmbientLight();
      xSemaphoreGive(i2cMutex);

      if (gyroOk) {
        if (firstRead) { uiX = rawX; uiY = rawY; firstRead = false; }
        
        // UI Visual Smoothing Filter
        uiX = (uiX * 0.6) + (rawX * 0.4); uiY = (uiY * 0.6) + (rawY * 0.4);
        
        // Long-term 24h mechanical drift compensation
        bootZeroX = (bootZeroX * 0.9995) + (uiX * 0.0005);
        bootZeroY = (bootZeroY * 0.9995) + (uiY * 0.0005);

        float diffX = abs(rawX - bootZeroX); float diffY = abs(rawY - bootZeroY);
        
        // Tip-over detection with time debounce
        if (diffX > 30.0 || diffY > 30.0) {
          consecutiveTilts++; if (consecutiveTilts >= 3) shakeTimer = 8; 
        } else { consecutiveTilts = 0; }
      }
      
      isTilting = (shakeTimer > 0);
      if (shakeTimer > 0) shakeTimer--;
      if (!isGyroAlarmEnabled) isTilting = false;
      isBoxOpen = (tempLux > 30);
    }

    // 2. THERMAL CONTROL LOOP & EDGE AI (2 Seconds)
    if (currentMillis - previousPDTime >= 2000) {
      float deltaTime = (currentMillis - previousPDTime) / 1000.0;
      previousPDTime = currentMillis;

      xSemaphoreTake(i2cMutex, portMAX_DELAY);
      tempC = htu.readTemperature();
      hum = htu.readHumidity();
      xSemaphoreGive(i2cMutex);

      if (tempC > 0.0) {
        // Thermal low-pass filter to kill micro-noise before AI processing
        if (smoothedTempC == 0.0) smoothedTempC = tempC;
        smoothedTempC = (smoothedTempC * 0.8) + (tempC * 0.2);

        // Shift register for Chebyshev Polynomials
        t_n2 = t_n1; t_n1 = t_n0; t_n0 = smoothedTempC;
        if (t_n2 > 0) aiReady = true;

        if (aiReady) {
           float velocity = (t_n0 - t_n1) / deltaTime; 
           float acceleration = (t_n0 - 2*t_n1 + t_n2) / (deltaTime * deltaTime);
           // 2nd Order Polynomial Forecast (15s into the future)
           projectedTemp = t_n0 + (velocity * 15.0) + (0.5 * acceleration * 225.0);
           dT = velocity; 
        } else { projectedTemp = smoothedTempC; dT = 0; }

        float error = projectedTemp - (float)localTarget;
        
        // 1. EARLY SHUTDOWN: Save energy if within 0.3C of target
        if (error < 0.3) {
           finalDuty = 0;
           fanWasOff = true;
        } 
        // 2. WAKE-UP DEADBAND: Hysteresis logic prevents micro-starts
        else if (error < 0.8 && finalDuty == 0) {
           finalDuty = 0;
           fanWasOff = true;
        } 
        // 3. STANDARD PD CONTROL
        else {
           if (error < 0) error = 0; 
           int pdOutput = (int)((Kp * error) + (Kd * max(0.0f, dT)));
           
           if (pdOutput > 0) {
             if (fanWasOff) {
               kickstartTime = currentMillis;
               fanWasOff = false;
             }
             
             // KICKSTART: 500ms at MAX power to overcome motor static friction
             if (currentMillis - kickstartTime < 500) {
               finalDuty = 255; 
             } else {
               // Progressive MAP with raised floor (115) to prevent stalling
               finalDuty = map(pdOutput, 1, 255, 115, 255); 
             }
             
           } else {
             finalDuty = 0;
             fanWasOff = true;
           }
        }
        finalDuty = constrain(finalDuty, 0, 255);
      }
    }

    bool overrideActive = false; int overrideDuty = 0; bool buzzOn = false; bool sysMuted = false;

    // ALGORITHMIC CURRENT ESTIMATION MODEL (mA)
    float calcCurrent = 160.0; // Base consumption (ESP32 + WiFi + Sensors)
    calcCurrent += ((float)finalDuty / 255.0) * 220.0; // Dynamic fan draw
    if (isTilting || buzzOn || tempC > localTarget + 5.0) calcCurrent += 30.0; // Buzzer draw
    calcCurrent += (random(-30, 30) / 10.0); // Simulated measurement noise

    // Update Global Structure
    if (xSemaphoreTake(dataMutex, portMAX_DELAY) == pdTRUE) {
      sysData.currentTemp = tempC; sysData.currentHum = hum;
      sysData.tiltX = uiX; sysData.tiltY = uiY;      
      sysData.ambientLight = tempLux; sysData.tiltWarning = isTilting;
      sysData.intrusionWarning = isBoxOpen; sysData.rateOfChange = dT;
      sysData.cpuTemp = (temprature_sens_read() - 32) * 5.0 / 9.0;
      sysData.predictedTemp = projectedTemp; sysData.estimatedCurrent = calcCurrent;
      
      overrideActive = sysData.manualOverride; overrideDuty = sysData.manualFanDuty;
      buzzOn = sysData.buzzerActive; sysMuted = sysData.buzzerMuted;
      if (overrideActive) finalDuty = overrideDuty;
      sysData.currentFanDuty = finalDuty;
      xSemaphoreGive(dataMutex);
    }

    // 4. PHYSICAL ACTUATORS DISPATCH
    ledcWrite(MOSFET_PIN, finalDuty); ledcWrite(LED_TEST_PIN, finalDuty);
    
    if (sysMuted) { digitalWrite(BUZZER_PIN, LOW); } else {
      if (buzzOn) { digitalWrite(BUZZER_PIN, HIGH); } 
      else if (isTilting) { digitalWrite(BUZZER_PIN, (currentMillis / 100) % 2); } 
      else if (tempC > localTarget + 5.0) { digitalWrite(BUZZER_PIN, (currentMillis / 500) % 2); } 
      else { digitalWrite(BUZZER_PIN, LOW); }
    }
    vTaskDelay(pdTICKS_TO_MS(10));
  }
}

/* ==================================================================
 * CORE 1: OLED UI & OMNI-CONNECTIVITY
 * ================================================================== */
void Core1_UIAndComm(void *pvParameters) {
  unsigned long previousUiTime = 0;
  static int animCounter = 0;
  
  for (;;) {
    unsigned long currentMillis = millis();

    float tC = 0, tH = 0, tX = 0, tY = 0, tDT = 0, cTemp = 0, pTemp = 0, eCurr = 0;
    int tTgt = 30, tLux = 0, tDuty = 0;
    bool tWarn = false, tIntrusion = false;

    // Fetch variables for transmission
    if (xSemaphoreTake(dataMutex, portMAX_DELAY) == pdTRUE) {
      tC = sysData.currentTemp; tH = sysData.currentHum;
      tX = sysData.tiltX; tY = sysData.tiltY;
      tLux = sysData.ambientLight; tDuty = sysData.currentFanDuty;
      tWarn = sysData.tiltWarning; tIntrusion = sysData.intrusionWarning;
      tTgt = sysData.targetTemp; cTemp = sysData.cpuTemp;
      pTemp = sysData.predictedTemp; eCurr = sysData.estimatedCurrent;
      xSemaphoreGive(dataMutex);
    }

    // TELEMETRY PACKET (12 Fields)
    Serial.printf("%.2f|%.2f|%.0f|%.0f|%d|%d|%d|%d|%d|%.1f|%.2f|%.1f\n", tC, tH, tX, tY, tLux, tDuty, tWarn ? 1 : 0, tTgt, tIntrusion ? 1 : 0, cTemp, pTemp, eCurr);
    if (SerialBT.hasClient()) {
      SerialBT.printf("%.2f|%.2f|%.0f|%.0f|%d|%d|%d|%d|%d|%.1f|%.2f|%.1f\n", tC, tH, tX, tY, tLux, tDuty, tWarn ? 1 : 0, tTgt, tIntrusion ? 1 : 0, cTemp, pTemp, eCurr);
    }

    WiFiClient client = server.available();
    if (client) {
      if (client.connected()) {
        client.printf("%.2f|%.2f|%.0f|%.0f|%d|%d|%d|%d|%d|%.1f|%.2f|%.1f\n", tC, tH, tX, tY, tLux, tDuty, tWarn ? 1 : 0, tTgt, tIntrusion ? 1 : 0, cTemp, pTemp, eCurr);
        
        // Command Parser
        if (client.available()) {
          String request = client.readStringUntil('\n');
          request.trim();
          if (xSemaphoreTake(dataMutex, portMAX_DELAY) == pdTRUE) {
            if (request == "CMD:FAN_MAX") sysData.manualOverride = true, sysData.manualFanDuty = 255;
            else if (request == "CMD:FAN_AUTO") sysData.manualOverride = false;
            else if (request == "CMD:BUZZ_ON") sysData.buzzerActive = true;
            else if (request == "CMD:BUZZ_OFF") sysData.buzzerActive = false;
            else if (request == "CMD:MUTE_ON") sysData.buzzerMuted = true;
            else if (request == "CMD:MUTE_OFF") sysData.buzzerMuted = false;
            else if (request == "CMD:CALIBRATE") sysData.triggerCalibration = true;
            else if (request == "CMD:GYRO_ON") sysData.gyroAlarmEnabled = true;
            else if (request == "CMD:GYRO_OFF") sysData.gyroAlarmEnabled = false;
            else if (request.startsWith("CMD:SET_TGT:")) {
              int newTarget = request.substring(12).toInt();
              if(newTarget >= 20 && newTarget <= 60) sysData.targetTemp = newTarget;
            }
            xSemaphoreGive(dataMutex);
          }
        }
      }
      client.stop();
    }

    // OLED RENDER CYCLE (150ms)
    if (currentMillis - previousUiTime >= 150) {
      previousUiTime = currentMillis;
      
      for (int i = 0; i < GRAPH_WIDTH - 1; i++) fanHistory[i] = fanHistory[i + 1];
      fanHistory[GRAPH_WIDTH - 1] = map(tDuty, 0, 255, 0, 18);

      display.clearDisplay();
      
      // ROW 1: Temp, Humidity, and Current Draw
      display.setCursor(0, 0);
      display.print("T:"); display.print(tC, 1); display.print("C ");
      display.print("H:"); display.print(tH, 0); display.print("% ");
      display.print("mA:"); display.print(eCurr, 0);

      // ROW 2: System Targets & Health
      display.setCursor(0, 12);
      display.print("Tgt:"); display.print(tTgt); display.print("C ");
      display.print("CPU:"); display.print(cTemp, 0); display.print("C ");
      display.print("AI:"); display.print(pTemp, 0); display.print("C");

      // ROW 3: I2C Sensors
      display.setCursor(0, 24);
      display.print("Lx:"); display.print(tLux);
      display.setCursor(55, 24);
      display.print("X:"); display.print(tX, 0);
      display.setCursor(95, 24);
      display.print("Y:"); display.print(tY, 0);

      // BOTTOM HALF: Alarms and Fan Animation
      if (tIntrusion) {
        if (toggleWarning) {
          display.fillRect(0, 40, SCREEN_WIDTH, 24, SSD1306_WHITE);
          display.setTextColor(SSD1306_BLACK);
          display.setCursor(10, 48);
          display.print("[!] BOX INTRUSION");
        }
        toggleWarning = !toggleWarning;
        display.setTextColor(SSD1306_WHITE); 
      } else if (tWarn) {
        if (toggleWarning) {
          display.fillRect(0, 40, SCREEN_WIDTH, 24, SSD1306_WHITE);
          display.setTextColor(SSD1306_BLACK);
          display.setCursor(15, 48);
          display.print("[!] TIP-OVER ALERT");
        }
        toggleWarning = !toggleWarning;
        display.setTextColor(SSD1306_WHITE); 
      } else {
        int fanPercent = map(tDuty, 0, 255, 0, 100);
        display.setCursor(0, 42);
        display.print("FAN:"); display.print(fanPercent); display.print("% ");

        if (tDuty > 0) {
          int frameDelay = map(tDuty, 1, 255, 6, 0); 
          animCounter++;
          const char blades[] = {'|', '/', '-', '\\'};
          display.print("["); display.print(blades[animationFrame]); display.print("]");
          if (animCounter >= frameDelay) {
             animationFrame = (animationFrame + 1) % 4;
             animCounter = 0;
          }
        } else {
          display.print("[STOP]");
        }

        display.drawRect(0, 54, 76, 8, SSD1306_WHITE); 
        int barWidth = map(tDuty, 0, 255, 0, 72); 
        if (barWidth > 0) display.fillRect(2, 56, barWidth, 4, SSD1306_WHITE); 

        // Live Fan Telemetry Graph
        display.drawRect(84, 42, GRAPH_WIDTH + 2, 22, SSD1306_WHITE); 
        for (int i = 0; i < GRAPH_WIDTH - 1; i++) {
          int x1 = 85 + i;
          int y1 = 62 - fanHistory[i]; 
          int x2 = 85 + (i + 1);
          int y2 = 62 - fanHistory[i + 1];
          display.drawLine(x1, y1, x2, y2, SSD1306_WHITE);
        }
      }

      xSemaphoreTake(i2cMutex, portMAX_DELAY);
      display.display();
      xSemaphoreGive(i2cMutex);
    }
    vTaskDelay(pdTICKS_TO_MS(50));
  }
}

void setup() {
  Serial.begin(115200);
  Wire.begin();
  Wire.setClock(100000); 

  if(!display.begin(SSD1306_SWITCHCAPVCC, 0x3C)) while(1);
  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);
  display.setTextSize(1);
  display.setCursor(0, 10);
  display.println("MYOSA MULTI-CORE RTOS");
  display.display();

  ledcAttach(MOSFET_PIN, PWM_FREQ, PWM_RESOLUTION);
  ledcAttach(LED_TEST_PIN, PWM_FREQ, PWM_RESOLUTION);
  pinMode(BUZZER_PIN, OUTPUT);
  digitalWrite(BUZZER_PIN, LOW);

  htu.begin();
  Ag.begin();
  Lpg.begin();
  SerialBT.begin("EduNode_BT"); 

  sysData.manualOverride = false;
  sysData.buzzerActive = false;
  sysData.buzzerMuted = false;
  sysData.currentFanDuty = 0;
  sysData.targetTemp = 30; 
  sysData.triggerCalibration = false; 
  sysData.gyroAlarmEnabled = false;  
  
  dataMutex = xSemaphoreCreateMutex();
  i2cMutex = xSemaphoreCreateMutex();

  WiFi.mode(WIFI_AP_STA);
  WiFi.begin(ssid, password);
  
  display.setCursor(0, 30);
  display.println("Connecting Wi-Fi...");
  display.display();

  int retries = 0;
  while (WiFi.status() != WL_CONNECTED && retries < 20) { 
    delay(500);
    retries++;
  }

  display.clearDisplay();
  display.setCursor(0, 10);
  
  if (WiFi.status() == WL_CONNECTED) {
    currentIP = WiFi.localIP().toString();
    display.print("Network OK!");
  } else {
    WiFi.mode(WIFI_AP);
    WiFi.softAP("EduNode_AP", "12345678"); 
    currentIP = WiFi.softAPIP().toString();
    display.print("AP MODE ACTIVE!");
  }
  
  display.setCursor(0, 25);
  display.print("IP: "); display.print(currentIP);
  display.display();
  
  server.begin();
  delay(3000);

  xTaskCreatePinnedToCore(Core0_SensorsAndControl, "Core0Task", 4096, NULL, 3, &Core0Task, 0);
  xTaskCreatePinnedToCore(Core1_UIAndComm, "Core1Task", 4096, NULL, 2, &Core1Task, 1);
}

void loop() { vTaskDelete(NULL); }
