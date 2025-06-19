import time
import board
import digitalio
import rotaryio
import asyncio
import keypad
import alarm
import microcontroller
import _bleio

import adafruit_ble
from adafruit_ble.advertising import Advertisement
from adafruit_ble.advertising.standard import ProvideServicesAdvertisement
from adafruit_ble.services.standard.hid import HIDService
from adafruit_hid.keyboard_layout_us import KeyboardLayoutUS
from adafruit_ble.services.standard.device_info import DeviceInfoService

from adafruit_hid.keyboard import Keyboard
from adafruit_hid.keycode import Keycode

print("Booting")

# Configuration constants
INACTIVITY_TIMEOUT = 5 * 60  # 5 minutes
LOOP_DELAY_CONNECTED = 0.01
LOOP_DELAY_DISCONNECTED = 0.1  # Reduced from 1 second
LONG_PRESS_DURATION = 5.0  # 5 seconds for long press
SCROLL_DELAY = 0.20  # 200ms batching delay
scroll_buffer = 0
scroll_timer = 0
last_scroll_direction = 0


# Initialize hardware
led = digitalio.DigitalInOut(board.LED)
led.direction = digitalio.Direction.OUTPUT
led.value = False

def init_hardware():
    """Initialize or reinitialize hardware objects"""
    global click, enc
    try:
        click = digitalio.DigitalInOut(board.D10)
        click.direction = digitalio.Direction.INPUT
        click.pull = digitalio.Pull.UP
        
        enc = rotaryio.IncrementalEncoder(board.D7, board.D8)
        return True
    except Exception as e:
        print(f"Hardware init failed: {e}")
        return False

def init_ble():
    """Initialize BLE services and advertising"""
    try:
        hid = HIDService()
        kbd = Keyboard(hid.devices)
        kbd_layout = KeyboardLayoutUS(kbd)
        
        device_info = DeviceInfoService(
            software_revision="1.1", 
            manufacturer="Stony Brook University"
        )
        
        # Create advertisement with both HID and device info services
        advertisement = ProvideServicesAdvertisement(hid, device_info)
        advertisement.appearance = 961
        
        unique_id = microcontroller.cpu.uid
        device_id = "{:02X}{:02X}".format(unique_id[-2], unique_id[-1])
        
        scan_response = Advertisement()
        scan_response.complete_name = f"Dial Scroll {device_id}"
        
        ble = adafruit_ble.BLERadio()
        
        return ble, kbd, kbd_layout, advertisement, scan_response, device_id
    except Exception as e:
        print(f"BLE init failed: {e}")
        return None, None, None, None, None, None, None

def safe_enter_deep_sleep():
    """Safely enter deep sleep with proper cleanup"""
    global click, enc, ble, kbd, kbd_layout, advertisement, scan_response, device_id
    
    print("Entering deep sleep...")
    led.value = False
    
    try:
        # Disconnect BLE safely
        if ble and ble.connected:
            print("Disconnecting BLE connections...")
            for connection in ble.connections:
                try:
                    connection.disconnect()
                    time.sleep(0.1)
                except:
                    pass  # Ignore disconnect errors
        
        # Stop advertising and deinitialize BLE radio completely
        if ble:
            try:
                if ble.advertising:
                    print("Stopping BLE advertising...")
                    ble.stop_advertising()
                    time.sleep(0.1)
                
                # Release the BLE adapter
                print("Releasing BLE adapter...")
                if _bleio.adapter:
                    _bleio.set_adapter(None)
                    time.sleep(0.1)
            except Exception as e:
                print(f"BLE cleanup error: {e}")
        
        # Nullify BLE objects
        ble = None
        # mouse = None
        kbd = None
        kbd_layout = None
        advertisement = None
        scan_response = None
        device_id = None

        # Deinitialize hardware objects
        try:
            if click:
                click.deinit()
        except:
            pass
        
        try:
            if enc:
                enc.deinit()
        except:
            pass
        
        # Create pin alarms for wakeup
        # Using both edge detection for better encoder response
        click_alarm = alarm.pin.PinAlarm(pin=board.D10, value=False, pull=True)
        enc_a_alarm = alarm.pin.PinAlarm(pin=board.D7, value=False, pull=True)
        enc_b_alarm = alarm.pin.PinAlarm(pin=board.D8, value=False, pull=True)
        
        # Enter deep sleep
        alarm.exit_and_deep_sleep_until_alarms(
            click_alarm,
            enc_a_alarm,
            enc_b_alarm
        )
        
    except Exception as e:
        print(f"Deep sleep error: {e}")
        # If deep sleep fails, try to reinitialize hardware
        time.sleep(1)
        init_hardware()

# Initialize everything
if not init_hardware():
    print("Critical: Hardware initialization failed")
    while True:
        led.value = not led.value
        time.sleep(0.5)

ble_result = init_ble()
if ble_result[0] is None:
    print("Critical: BLE initialization failed")
    while True:
        led.value = not led.value
        time.sleep(0.5)

ble, kbd, kbd_layout, advertisement, scan_response, device_id = ble_result

time.sleep(0.1) # Added delay after successful BLE init

last_activity_time = time.monotonic()

# Check if waking from deep sleep
if alarm.wake_alarm is not None:
    print(f"Waking from deep sleep: {alarm.wake_alarm}")
    last_activity_time = time.monotonic()  # Reset activity timer

pending_single_click = False
single_click_timer = 0
double_click_threshold = 0.5  # seconds
last_click_time = 0
click_count = 0

# Initialize tracking variables
last_click = click.value
last_position = enc.position
button_press_start_time = None
long_press_detected = False
was_connected = ble.connected # For BLE connection prints

# Start advertising if not connected
try:
    if not ble.connected:
        print(f"Starting advertising: {device_id}")
        ble.start_advertising(advertisement, scan_response)
    else:
        print("Already connected")
except Exception as e:
    print(f"Advertising error: {e}")

print("Entering main loop")

# Main loop
while True:
    try:
        current_time = time.monotonic()
        
        # Check for inactivity timeout
        if current_time - last_activity_time > INACTIVITY_TIMEOUT:
            safe_enter_deep_sleep()
        
        # Read current input states
        try:
            position = enc.position
            click_value = click.value
        except Exception as e:
            print(f"Input read error: {e}")
            # Try to reinitialize hardware
            if init_hardware():
                last_click = click.value
                last_position = enc.position
            time.sleep(LOOP_DELAY_CONNECTED)
            continue
        
        # Handle encoder movement
        if last_position != position:
            delta = position - last_position

            # Detect direction change
            new_direction = 1 if delta > 0 else -1
            if new_direction != last_scroll_direction and scroll_buffer != 0:
                # Flush existing buffer immediately
                print("Direction changed, flushing buffer early")
                movement = scroll_buffer
                scroll_buffer = 0
                scroll_timer = 0

                if ble.connected:
                    try:
                        if abs(movement) >= 3:
                            if movement > 0:
                                print(f"Gesture: RIGHT ({movement} units)")
                                kbd.press(Keycode.RIGHT_ARROW)
                                kbd.release(Keycode.RIGHT_ARROW)
                            else:
                                print(f"Gesture: LEFT ({movement} units)")
                                kbd.press(Keycode.LEFT_ARROW)
                                kbd.release(Keycode.LEFT_ARROW)
                        else:
                            if movement > 0:
                                print(f"Scrolled: DOWN ({movement} units)")
                                for _ in range(movement):
                                    kbd.press(Keycode.DOWN_ARROW)
                                    kbd.release(Keycode.DOWN_ARROW)
                            else:
                                print(f"Scrolled: UP ({-movement} units)")
                                for _ in range(-movement):
                                    kbd.press(Keycode.UP_ARROW)
                                    kbd.release(Keycode.UP_ARROW)
                    except Exception as e:
                        print(f"Buffered scroll error: {e}")

            # Continue normal buffering
            scroll_buffer += delta
            last_scroll_direction = new_direction
            last_position = position
            last_activity_time = current_time
            scroll_timer = current_time  # reset batching timer


        # Process buffered scroll after delay
        if abs(scroll_buffer) > 0 and (current_time - scroll_timer) >= SCROLL_DELAY:
            movement = scroll_buffer
            scroll_buffer = 0  # reset buffer

            if ble.connected:
                try:
                    if abs(movement) >= 3:
                        if movement > 0:
                            print(f"Gesture: RIGHT ({movement} units)")
                            kbd.press(Keycode.RIGHT_ARROW)
                            kbd.release(Keycode.RIGHT_ARROW)
                        else:
                            print(f"Gesture: LEFT ({movement} units)")
                            kbd.press(Keycode.LEFT_ARROW)
                            kbd.release(Keycode.LEFT_ARROW)
                    else:
                        if movement > 0:
                            print(f"Scrolled: DOWN ({movement} units)")
                            for _ in range(movement):
                                kbd.press(Keycode.DOWN_ARROW)
                                kbd.release(Keycode.DOWN_ARROW)
                        else:
                            print(f"Scrolled: UP ({-movement} units)")
                            for _ in range(-movement):
                                kbd.press(Keycode.UP_ARROW)
                                kbd.release(Keycode.UP_ARROW)
                except Exception as e:
                    print(f"Buffered scroll error: {e}")

        
        # Handle button press/release and long press detection
        if last_click != click_value:
            if not click_value:  # Button pressed
                button_press_start_time = current_time
                long_press_detected = False
                last_activity_time = current_time

            else:  # Button released
                press_duration = current_time - button_press_start_time
                if press_duration >= LONG_PRESS_DURATION:
                    print("Long press detected - entering deep sleep...")
                    # Visual feedback for long press
                    led.value = False
                    time.sleep(0.1)
                    led.value = True
                    time.sleep(0.1)
                    led.value = False
                    time.sleep(0.1)
                    led.value = True
                    safe_enter_deep_sleep()
                else:
                    # Handle double-click
                    if current_time - last_click_time < double_click_threshold:
                        click_count += 1
                    else:
                        click_count = 1
                    last_click_time = current_time

                    if click_count == 3:
                        print("Triple click detected - requesting Accessibility Settings")
                        if ble.connected:
                            try:
                                    kbd.press(Keycode.F1)
                                    kbd.release(Keycode.F1)
                            except Exception as e:
                                print(f"Triple click (Accessibility Settings) error: {e}")
                        click_count = 0
                        pending_single_click = False
                    elif press_duration < 2.0:
                        # Wait before confirming it's a single click
                        pending_single_click = True
                        single_click_timer = current_time

                button_press_start_time = None
                long_press_detected = False
        
        # Handle delayed single click (to check if it's truly single)
        if pending_single_click and (current_time - single_click_timer) >= double_click_threshold:
            if click_count == 1:
                print("Click: ENTER")
                if ble.connected:
                    try:
                        kbd.press(Keycode.ENTER)
                        kbd.release(Keycode.ENTER)
                    except Exception as e:
                        print(f"Single click (ENTER) error: {e}")
            elif click_count == 2:
                print("Double click detected - sending BACK")
                if ble.connected:
                    try:
                        kbd.press(Keycode.ESCAPE)
                        kbd.release(Keycode.ESCAPE)
                    except Exception as e:
                        print(f"Double click (BACK) error: {e}")
            elif click_count == 3:
                print("Triple click detected - requesting Accessibility Settings")
                if ble.connected:
                    try:
                        kbd.press(Keycode.ALT, Keycode.SHIFT, Keycode.A)
                        kbd.release_all()
                    except Exception as e:
                        print(f"Triple click (Accessibility Settings) error: {e}")
            
            pending_single_click = False
            click_count = 0

        last_click = click_value
        
        # Track BLE connection state changes
        if ble.connected != was_connected:
            if ble.connected:
                print("BLE connected")
            else:
                print("BLE disconnected")
            was_connected = ble.connected
        
        # Handle BLE connection state
        if not ble.connected:
            # Visual indication of disconnected state
            if int(current_time * 2) % 2:
                led.value = True
            else:
                led.value = False

            # Always (re)start advertising immediately when disconnected
            try:
                ble.stop_advertising()  # Stop any previous advertising
            except Exception:
                pass  # Ignore if not advertising

            try:
                ble.start_advertising(advertisement, scan_response)
                print("BLE advertising (discoverable)...")
            except Exception as e:
                print(f"Re-advertising error: {e}")

            time.sleep(LOOP_DELAY_DISCONNECTED)
        else:
            # Connected - solid LED
            led.value = True
            time.sleep(LOOP_DELAY_CONNECTED)
    
    except Exception as e:
        print(f"Main loop error: {e}")
        time.sleep(LOOP_DELAY_CONNECTED)
        continue