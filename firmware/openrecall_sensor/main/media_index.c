#include "media_index.h"
#include <inttypes.h>
#include <stdio.h>
#include <string.h>

void snapshot_path(char *out, size_t cap, uint32_t boot_id, uint32_t rel_ts_ms, uint32_t seq) {
  snprintf(out, cap, "/sdcard/snapshots/%" PRIu32 "/%010" PRIu32 "_%" PRIu32 ".jpg",
           (uint32_t)boot_id, (uint32_t)rel_ts_ms, (uint32_t)seq);
}
void video_path(char *out, size_t cap, uint32_t boot_id, uint32_t rel_ts_ms, uint32_t seq) {
  snprintf(out, cap, "/sdcard/video/%" PRIu32 "/%010" PRIu32 "_%" PRIu32 ".mjpeg",
           (uint32_t)boot_id, (uint32_t)rel_ts_ms, (uint32_t)seq);
}
void manifest_line(char *out, size_t cap, const char *path, uint32_t rel_ts_ms, uint32_t boot_id, char kind) {
  snprintf(out, cap, "%s|%" PRIu32 "|%" PRIu32 "|%c\n", path, (uint32_t)rel_ts_ms, (uint32_t)boot_id, kind);
}
int manifest_parse(const char *line, char *path_out, size_t path_cap, uint32_t *rel_ts_ms, uint32_t *boot_id, char *kind) {
  /* path|rel_ts_ms|boot_id|kind\n. Field width is hardcoded to 79 (= NAME_MAX_LEN-1)
     so the manifest format stays deterministic and independent of the caller's buffer. */
  (void)path_cap;
  char kind_c;
  uint32_t ts, boot;
  /* path may contain no '|'; use %[^|] */
  int n = sscanf(line, "%79[^|]|%" SCNu32 "|%" SCNu32 "|%c",
                 path_out, &ts, &boot, &kind_c);
  if (n != 4) return -1;
  if (kind_c != 's' && kind_c != 'v') return -1;
  if (rel_ts_ms) *rel_ts_ms = ts;
  if (boot_id) *boot_id = boot;
  if (kind) *kind = kind_c;
  return 0;
}
int consumed_parse(const char *body, char names[][NAME_MAX_LEN], size_t *count, size_t max) {
  size_t n = 0;
  const char *p = body;
  while (*p) {
    const char *nl = strchr(p, '\n');
    size_t len = nl ? (size_t)(nl - p) : strlen(p);
    if (len == 0) { if (!nl) break; p = nl + 1; continue; }
    if (len >= NAME_MAX_LEN) return -1;
    if (n >= max) return -1;
    memcpy(names[n], p, len);
    names[n][len] = '\0';
    n++;
    if (!nl) break;
    p = nl + 1;
  }
  if (count) *count = n;
  return 0;
}
