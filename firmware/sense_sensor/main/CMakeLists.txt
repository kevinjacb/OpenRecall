# Sources are added here as each module lands. c6_packet.c is dependency-free
# (also built by the host contract test). REQUIRES grows with the modules:
#   driver (I2S PDM, SPI), esp_timer, bt (NimBLE), mbedtls (Ed25519), esp_psram.
#
# Note on the driver component name: in IDF 5.1.6 it's `driver`; in IDF 6.0+
# it was split into per-peripheral components (`esp_driver_i2s`,
# `esp_driver_gpio`, etc.). We're pinned to 5.1.6 (NimBLE 1.6) so `driver`
# is the right name. If you bump to 6.0+ later, change this to
# `esp_driver_i2s` AND adapt to the new per-peripheral split.
idf_component_register(
    SRCS
        "sense_sensor.c"
        "c6_packet.c"
        "audio_capture.c"
        "vad.c"
        "ring_buffer.c"
        "opus_stream.c"
        "ble_link.c"
        "ble_drain.c"
        "commands.c"
        "provisioning.c"
        "provisioning_core.c"
    INCLUDE_DIRS "."
    REQUIRES
        esp_timer
        driver
        esp_psram
        bt
        nvs_flash
        libsodium
        cjson
        opus
)
