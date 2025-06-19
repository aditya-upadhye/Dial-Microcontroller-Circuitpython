import time, board, digitalio, rotaryio, alarm, microcontroller, analogio, adafruit_ble
from adafruit_ble.advertising import Advertisement
from adafruit_ble.advertising.standard import ProvideServicesAdvertisement
from adafruit_ble.services.standard.hid import HIDService
from adafruit_ble.services.standard.device_info import DeviceInfoService
from adafruit_ble.services.standard import BatteryService
from adafruit_hid.mouse import Mouse
from adafruit_hid.keyboard import Keyboard
from adafruit_hid.keyboard_layout_us import KeyboardLayoutUS
from adafruit_hid.keycode import Keycode

print("Boot")
INACTIVITY_TIMEOUT, LOOP_DELAY_CONNECTED, LOOP_DELAY_DISCONNECTED, LONG_PRESS_DURATION = 300, 0.01, 0.1, 2.0
BATTERY_UPDATE_INTERVAL, MIN_BATT_VOLTAGE, MAX_BATT_VOLTAGE = 60, 3.0, 3.7

# Corrected pins for Seeed Studio XIAO nRF52840 Sense
# VBATT (P0.31) for sensing, READ_BATT_ENABLE (P0.14 / D9) to enable reading circuit.
BATTERY_SENSE_PIN_ID = microcontroller.pin.P0_31 # board.VOLTAGE_MONITOR often maps to this
BATTERY_ENABLE_PIN_ID = microcontroller.pin.P0_14 # board.D9 often maps to this
CLICK_PIN_ID = board.D10
ENCODER_A_PIN_ID = board.D7
ENCODER_B_PIN_ID = board.D8
LED_PIN_ID = board.LED # Assuming standard LED pin

led = digitalio.DigitalInOut(LED_PIN_ID); led.direction = digitalio.Direction.OUTPUT; led.value = False
click, enc, battery_sense_pin, battery_enable_pin = None, None, None, None
ble, mouse, advertisement, scan_response, battery_service = None, None, None, None, None

hardware_initialized = False

# Helper function to safely deinitialize an object
def _safe_deinit(obj):
    if obj:
        try: obj.deinit()
        except: pass

def _read_and_update_battery_info(current_time_val):
    global last_battery_update_time, battery_service, battery_sense_pin, battery_enable_pin
    batt_level, batt_voltage = 0, 0.0
    if battery_sense_pin and battery_enable_pin:
        try:
            battery_enable_pin.value = False; time.sleep(0.01); adc_value = battery_sense_pin.value; battery_enable_pin.value = True
            adc_voltage = (adc_value / 65535) * battery_sense_pin.reference_voltage
            actual_voltage = adc_voltage * (1510.0 / 510.0)
            clamped_voltage = max(MIN_BATT_VOLTAGE, min(MAX_BATT_VOLTAGE, actual_voltage))
            batt_level = int(max(0, min(100, ((clamped_voltage - MIN_BATT_VOLTAGE) / (MAX_BATT_VOLTAGE - MIN_BATT_VOLTAGE)) * 100)))
            batt_voltage = actual_voltage
        except Exception as e: print(f"Battery read error: {e}"); battery_enable_pin.value = True; batt_level, batt_voltage = 0,0.0
    
    if battery_service: battery_service.level = batt_level
    print(f"Battery: {batt_level}% ({batt_voltage:.1f}V)"); last_battery_update_time = current_time_val

def _critical_halt(message, blink_delay=0.5):
    print(message)
    while True: led.value = not led.value; time.sleep(blink_delay)

try:
    click = digitalio.DigitalInOut(CLICK_PIN_ID); click.direction = digitalio.Direction.INPUT; click.pull = digitalio.Pull.UP
    enc = rotaryio.IncrementalEncoder(ENCODER_A_PIN_ID, ENCODER_B_PIN_ID)
    battery_enable_pin = digitalio.DigitalInOut(BATTERY_ENABLE_PIN_ID); battery_enable_pin.direction = digitalio.Direction.OUTPUT; battery_enable_pin.value = True
    battery_sense_pin = analogio.AnalogIn(BATTERY_SENSE_PIN_ID)
    hardware_initialized = True
except Exception as e: print(f"Hardware initialization error: {e}")

if not hardware_initialized:
    _critical_halt("Critical: Hardware Failure. Halting.")

ble_initialized = False
if hardware_initialized:
    try:
        hid_service = HIDService()
        mouse = Mouse(hid_service.devices)
        keyboard = Keyboard(hid_service.devices)
        keyboard_layout = KeyboardLayoutUS(keyboard)
        device_info_service = DeviceInfoService(software_revision="1.1", manufacturer="SBU")
        battery_service = BatteryService()
        advertisement = ProvideServicesAdvertisement(hid_service, device_info_service, battery_service); advertisement.appearance = 962
        unique_id = microcontroller.cpu.uid; device_id_suffix = "{:02X}{:02X}".format(unique_id[-2], unique_id[-1])
        scan_response = Advertisement(); scan_response.complete_name = f"Dial Mouse {device_id_suffix}"
        ble = adafruit_ble.BLERadio()
        ble_initialized = True
    except Exception as e: print(f"BLE initialization error: {e}")

if not ble_initialized and hardware_initialized:
    _critical_halt("Critical: BLE Failure. Halting.")

last_activity_time = time.monotonic()
last_battery_update_time = 0.0

if alarm.wake_alarm: print(f"Wake: {alarm.wake_alarm}"); last_activity_time = time.monotonic()

button_press_start_time = None # Time when button was pressed
long_press_mode_active = False # True when long press detected and preparing for sleep
was_connected = ble.connected if ble else False
last_position = enc.position if enc else 0 # Initialize last_position

try:
    if ble and not ble.connected: print(f"Advertising: {scan_response.complete_name}"); ble.start_advertising(advertisement, scan_response)
    elif ble and ble.connected: print("BLE Connected")
except Exception as e: print(f"Advertising error: {e}")

print("Loop")

while True:
    try:
        current_time = time.monotonic()

        if long_press_mode_active: # Special mode: waiting for button release to sleep
            if click and click.value: # Button released
                print("Long Press released. Entering deep sleep."); led.value = False
                _safe_deinit(click)
                # Other peripherals (enc, battery pins) deinitialized when long_press_mode_active was set.
                
                final_click_alarm = alarm.pin.PinAlarm(CLICK_PIN_ID, value=False, pull=True)
                alarm.exit_and_deep_sleep_until_alarms(final_click_alarm)
            else: # Button still held or click object is None
                time.sleep(0.01) # Yield/debounce
            continue # Skip all other processing in the main loop

        if current_time - last_activity_time > INACTIVITY_TIMEOUT:
            print("Inactivity sleep (Wake on D10 only)"); led.value = False
            if ble and ble.connected:
                for connection in ble.connections: 
                    try: connection.disconnect()
                    except: pass
            if ble: ble.stop_advertising()
            
            # Deinitialize hardware components before sleep
            _safe_deinit(click)
            _safe_deinit(enc)
            _safe_deinit(battery_sense_pin)
            if battery_enable_pin:
                try: battery_enable_pin.value = False; battery_enable_pin.deinit();
                except: pass # Turn off and deinit

            # For inactivity, wake on click pin press
            # The diagnostic print for D10 only was here, I'll remove it as this is now the defined behavior
            # print("Diagnostic: Inactivity sleep on D10 only") 
            deep_sleep_click_alarm = alarm.pin.PinAlarm(CLICK_PIN_ID, False, pull=True)
            alarm.exit_and_deep_sleep_until_alarms(deep_sleep_click_alarm)
        
        try:
            position, click_value = enc.position, click.value
        except Exception as e: print(f"Input error: {e}. Continuing..."); time.sleep(LOOP_DELAY_CONNECTED if ble and ble.connected else LOOP_DELAY_DISCONNECTED); continue
        
        if last_position != position:
            movement = last_position - position; last_position = position; last_activity_time = current_time
            if ble and ble.connected:
                try:
                    mouse.move(y=int(movement))
                    for i in range(abs(movement)):
                        if (movement > 0):
                            keyboard_layout.press(Keycode.U)
                            keyboard_layout.release(Keycode.U)
                        elif (movement < 0):
                            keyboard_layout.press(Keycode.D)
                            keyboard_layout.release(Keycode.D)
                except Exception as e: print(f"Mouse move error: {e}")
        
        # New button logic for click and long press detection
        if not click_value: # Button is currently pressed
            if button_press_start_time is None: # Button was just pressed
                button_press_start_time = last_activity_time = current_time # Register activity
            # else: button is being held, check duration below
            if button_press_start_time is not None and (current_time - button_press_start_time) >= LONG_PRESS_DURATION:
                # Long press duration reached
                print("Long Press detected. Preparing for sleep on release.")
                led.value = True # Indicate long press processing

                if ble and ble.connected:
                    print("Disconnecting BLE for Long Press...")
                    for conn in ble.connections: 
                        try: conn.disconnect()
                        except: pass # Ignore errors
                if ble: print("Stopping BLE advertising for Long Press..."); ble.stop_advertising()
                
                print("Deinitializing peripherals for Long Press...")
                _safe_deinit(enc)
                _safe_deinit(battery_sense_pin)
                if battery_enable_pin:
                    try: battery_enable_pin.value = False; battery_enable_pin.deinit();
                    except: pass
                
                long_press_mode_active = True # Engage special mode (wait for release)
                button_press_start_time = None # Prevent re-triggering LP detection, release will be handled by long_press_mode_active block
        else: # Button is currently released
            if button_press_start_time is not None: # Button was just released (and wasn't a long press that set long_press_mode_active)
                # This path is for a short click release
                if ble and ble.connected:
                    try:
                        mouse.click(Mouse.LEFT_BUTTON); print("Click");
                        keyboard_layout.press(Keycode.ENTER)
                        keyboard_layout.release(Keycode.ENTER)
                    except Exception as e: print(f"Mouse click error: {e}")
                last_activity_time = current_time # Register activity
                button_press_start_time = None # Reset for next press

        # BLE connection status change handling (only if not in long_press_mode)
        if ble and ble.connected != was_connected:
            print("BLE Connected" if ble.connected else "BLE Disconnected")
            if ble.connected: # Still need this for the battery update
                _read_and_update_battery_info(current_time)
            was_connected = ble.connected
        
        # Main operational states: advertising or connected (only if not in long_press_mode)
        if ble and not ble.connected:
            led.value = bool(int(current_time * 2) % 2) # Blinking LED when not connected
            try: 
                if not ble.advertising: ble.start_advertising(advertisement, scan_response)
            except Exception as e: print(f"Re-advertising error: {e}")
            time.sleep(LOOP_DELAY_DISCONNECTED)
        elif ble and ble.connected:
            led.value = True # Solid LED when connected
            if current_time - last_battery_update_time > BATTERY_UPDATE_INTERVAL:
                # Perform periodic battery update
                _read_and_update_battery_info(current_time)
            time.sleep(LOOP_DELAY_CONNECTED)
        elif ble is None and hardware_initialized:
            _critical_halt("Critical: BLE None. Halting.", 0.2)

    except Exception as e:
        _critical_halt(f"Loop error: {e}\\nDevice restart?", 0.2)