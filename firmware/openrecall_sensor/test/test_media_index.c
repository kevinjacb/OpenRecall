#include "media_index.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

static int failures = 0;
static void check(const char *n, int ok) { printf("  [%s] %s\n", ok?"PASS":"FAIL", n); if(!ok) failures++; }

static void test_paths(void) {
  printf("test_paths\n");
  char buf[80];
  snapshot_path(buf, sizeof buf, 7, 5000, 3);
  check("snapshot path", strcmp(buf, "/sdcard/snapshots/7/0000005000_3.jpg")==0);
  video_path(buf, sizeof buf, 7, 123456, 11);
  check("video path", strcmp(buf, "/sdcard/video/7/0000123456_11.mjpeg")==0);
  /* boot 0 still works (pre-boot edge) */
  snapshot_path(buf, sizeof buf, 0, 0, 0);
  check("zero boot/seq", strcmp(buf, "/sdcard/snapshots/0/0000000000_0.jpg")==0);
}

static void test_manifest_line(void) {
  printf("test_manifest_line\n");
  char line[96];
  manifest_line(line, sizeof line, "/sdcard/snapshots/7/0000005000_3.jpg", 5000, 7, 's');
  check("snapshot line", strcmp(line, "/sdcard/snapshots/7/0000005000_3.jpg|5000|7|s\n")==0);
  char p[80]; uint32_t ts, boot; char kind;
  check("parse round-trip", manifest_parse(line, p, sizeof p, &ts, &boot, &kind)==0
        && strcmp(p, "/sdcard/snapshots/7/0000005000_3.jpg")==0 && ts==5000 && boot==7 && kind=='s');
  check("parse malformed", manifest_parse("no|pipes|here\n", p, sizeof p, &ts, &boot, &kind)<0);
  check("parse wrong kind", manifest_parse("a|1|2|z\n", p, sizeof p, &ts, &boot, &kind)<0);
}

static void test_consumed_parse(void) {
  printf("test_consumed_parse\n");
  char names[8][NAME_MAX_LEN]; size_t n;
  const char *body = "/sdcard/snapshots/7/0000005000_3.jpg\n/sdcard/video/7/0000123456_11.mjpeg\n";
  check("parse 2 names", consumed_parse(body, names, &n, 8)==0 && n==2
        && strcmp(names[0], "/sdcard/snapshots/7/0000005000_3.jpg")==0
        && strcmp(names[1], "/sdcard/video/7/0000123456_11.mjpeg")==0);
  check("too many -> err", consumed_parse(body, names, &n, 1)<0);
  check("empty body -> 0", consumed_parse("", names, &n, 8)==0 && n==0);
}

int main(void) {
  test_paths(); test_manifest_line(); test_consumed_parse();
  if (failures) { printf("FAIL: %d\n", failures); return 1; }
  printf("ALL PASS\n"); return 0;
}