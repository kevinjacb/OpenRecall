# OpenRecall self-test firmware. Reuses the production drivers by reference:
# audio_capture.c (I2S mics) and camera.c (OV2640 JPEG) are added as SRCS from
# ../../openrecall_sensor/main so the rig tests the REAL product code, not a copy.
# config.h (and CAM_PIN_*/FRAME_SAMPLES) is shared via INCLUDE_DIRS. Pinned to
# IDF 5.1.6 (`driver` component for GPIO/I2S; `esp_adc` for oneshot ADC +
# curve-fitting calibration; `esp32-camera` for the OV2640 driver).
#
# NOTE: paths in idf_component_register are relative to THIS file (main/), so to
# reach ../openrecall_sensor/main (a sibling project under firmware/) we go up
# two levels: ../../openrecall_sensor/main.
idf_component_register(
    SRCS
        "selftest_main.c"
        "test_battery.c"
        "test_button.c"
        "test_audio.c"
        "test_camera.c"
        "../../openrecall_sensor/main/audio_capture.c"
        "../../openrecall_sensor/main/camera.c"
    INCLUDE_DIRS
        "."
        "../../openrecall_sensor/main"
    REQUIRES
        driver
        esp_adc
        esp32-camera
)