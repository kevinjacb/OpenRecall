# OpenRecall self-test firmware. Reuses the production drivers by reference:
# audio_capture.c (I2S mics) and camera.c (OV2640 JPEG) are added as SRCS from
# ../openrecall_sensor/main so the rig tests the REAL product code, not a copy.
# config.h is shared via INCLUDE_DIRS. Pinned to IDF 5.1.6 (`driver` component).
idf_component_register(
    SRCS
        "selftest_main.c"
    INCLUDE_DIRS
        "."
    REQUIRES
        driver
)