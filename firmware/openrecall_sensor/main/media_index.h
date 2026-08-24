/*
 * P4b media index — pure-C manifest + path math (no ESP deps, host-tested).
 * Paths, manifest lines, and the POST /consumed body are deterministic strings
 * so the transfer HTTP server and the host test share one source of truth.
 */
#pragma once
#include <stddef.h>
#include <stdint.h>

#define NAME_MAX_LEN 80   /* max path length in a manifest line / consumed body */

void snapshot_path(char *out, size_t cap, uint32_t boot_id, uint32_t rel_ts_ms, uint32_t seq);
void video_path(char *out, size_t cap, uint32_t boot_id, uint32_t rel_ts_ms, uint32_t seq);
void manifest_line(char *out, size_t cap, const char *path, uint32_t rel_ts_ms, uint32_t boot_id, char kind);
int  manifest_parse(const char *line, char *path_out, size_t path_cap, uint32_t *rel_ts_ms, uint32_t *boot_id, char *kind);
int  consumed_parse(const char *body, char names[][NAME_MAX_LEN], size_t *count, size_t max);
