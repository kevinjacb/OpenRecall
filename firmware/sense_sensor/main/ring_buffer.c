#include "ring_buffer.h"

#include "config.h"
#include "esp_check.h"
#include "esp_heap_caps.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"  // taskENTER_CRITICAL / taskEXIT_CRITICAL macros

#include <string.h>

static const char *TAG = "ring";

typedef struct {
  uint32_t rel_ts_ms;
  uint8_t  vad_state;
  uint8_t  len;
  uint8_t  data[MAX_OPUS_BYTES];
} ring_slot_t;

static ring_slot_t *s_slots;                 // RING_FRAMES slots in PSRAM
static uint32_t s_write_index;               // monotonic count of frames written
static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;

esp_err_t ring_buffer_init(void) {
  s_slots = heap_caps_calloc(RING_FRAMES, sizeof(ring_slot_t), MALLOC_CAP_SPIRAM);
  ESP_RETURN_ON_FALSE(s_slots != NULL, ESP_ERR_NO_MEM, TAG, "PSRAM alloc failed");
  s_write_index = 0;
  ESP_LOGI(TAG, "ring up: %d frames (%d s) in PSRAM, %u KB",
           RING_FRAMES, RING_SECONDS,
           (unsigned)(RING_FRAMES * sizeof(ring_slot_t) / 1024));
  return ESP_OK;
}

void ring_buffer_push(uint8_t vad_state, uint32_t rel_ts_ms, const uint8_t *data, uint8_t len) {
  // len is a uint8_t (<=255) and MAX_OPUS_BYTES is 256, so any frame fits the slot.
  _Static_assert(MAX_OPUS_BYTES >= 256, "slot must hold any u8-length frame");
  taskENTER_CRITICAL(&s_mux);
  ring_slot_t *slot = &s_slots[s_write_index % RING_FRAMES];
  slot->rel_ts_ms = rel_ts_ms;
  slot->vad_state = vad_state;
  slot->len = len;
  if (len > 0 && data != NULL) {
    memcpy(slot->data, data, len);
  }
  s_write_index++;  // publish last: a reader that sees the new index sees a full slot
  taskEXIT_CRITICAL(&s_mux);
}

uint32_t ring_buffer_write_index(void) {
  return s_write_index;
}

bool ring_buffer_get(uint32_t index, ring_frame_t *out) {
  bool ok = false;
  taskENTER_CRITICAL(&s_mux);
  uint32_t w = s_write_index;
  // Valid window is [w - RING_FRAMES, w). Guard the underflow when < RING_FRAMES.
  bool in_range = index < w && (w - index) <= RING_FRAMES;
  if (in_range) {
    const ring_slot_t *slot = &s_slots[index % RING_FRAMES];
    out->rel_ts_ms = slot->rel_ts_ms;
    out->vad_state = slot->vad_state;
    out->len = slot->len;
    out->data = slot->data;
    ok = true;
  }
  taskEXIT_CRITICAL(&s_mux);
  return ok;
}
