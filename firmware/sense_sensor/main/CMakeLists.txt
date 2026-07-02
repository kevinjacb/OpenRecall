# Sources are added here as each module lands. c6_packet.c is dependency-free
# (also built by the host contract test). REQUIRES grows with the modules:
#   driver (I2S PDM, SPI), esp_timer, bt (NimBLE), mbedtls (Ed25519), esp_psram.
idf_component_register(
    SRCS
        "sense_sensor.c"
        "c6_packet.c"
        "audio_capture.c"
        "vad.c"
        "ring_buffer.c"
        "ble_link.c"
        "commands.c"
    INCLUDE_DIRS "."
    REQUIRES
        esp_timer
        esp_driver_i2s
        esp_psram
        bt
        nvs_flash
        libsodium
        cjson
)
