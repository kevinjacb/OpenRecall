/* firmware/sense_sensor/main/provisioning.h */
#ifndef PROVISIONING_H
#define PROVISIONING_H
#include <stdint.h>

/* Forward declaration so the access-callback prototypes below match NimBLE's
 * ble_gatt_access_fn signature exactly (see ble_link.c). */
struct ble_gatt_access_ctxt;

int provisioning_init(void);              /* call once on boot, after nvs_flash_init */
uint8_t provisioning_state_byte(void);    /* 0 unprovisioned, 1 provisioned */

/* Push the current state byte to the STATE characteristic (no-op if no client is
 * subscribed). Implemented in ble_link.c where the notify machinery + handle live. */
void provisioning_notify_state(void);

/* GATT access callbacks (referenced by ble_link.c's GATT table).
 * Signature matches NimBLE's ble_gatt_access_fn (4 args, like ble_link.c's
 * command_write_access / chr_noop_access). */
int prov_state_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt,
                      void *arg);
int prov_key_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt,
                    void *arg);
int prov_reset_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt,
                      void *arg);

#endif