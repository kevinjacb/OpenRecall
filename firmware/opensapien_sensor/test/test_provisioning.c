/* firmware/opensapien_sensor/test/test_provisioning.c */
#include <string.h>
#include <assert.h>
#include <stdio.h>
#include "../main/provisioning_core.h"

static uint8_t s_key[32];
static uint8_t s_prov;

static int stub_get(uint8_t out_key[32], uint8_t *out_provisioned) {
    if (!s_prov) return 1;
    memcpy(out_key, s_key, 32);
    *out_provisioned = 1;
    return 0;
}
static int stub_set(const uint8_t key[32]) { memcpy(s_key, key, 32); s_prov = 1; return 0; }
static int stub_clear(void) { memset(s_key, 0, 32); s_prov = 0; return 0; }

static const prov_store_t stub_store = { stub_get, stub_set, stub_clear };

static void reset_store(void) { memset(s_key, 0, 32); s_prov = 0; }

int main(void) {
    /* fresh boot: unprovisioned */
    reset_store();
    assert(provisioning_core_init(&stub_store) == 0);
    assert(provisioning_core_state() == PROV_UNPROVISIONED);

    /* applying a key when unprovisioned succeeds and flips state */
    uint8_t k[32]; for (int i = 0; i < 32; i++) k[i] = (uint8_t)(i + 1);
    assert(provisioning_core_apply_key(k) == 0);
    assert(provisioning_core_state() == PROV_PROVISIONED);
    assert(memcmp(provisioning_core_server_key(), k, 32) == 0);

    /* applying again while provisioned is rejected */
    uint8_t k2[32]; memset(k2, 9, 32);
    assert(provisioning_core_apply_key(k2) == -1);
    assert(provisioning_core_state() == PROV_PROVISIONED);
    assert(memcmp(provisioning_core_server_key(), k, 32) == 0); /* unchanged */

    /* factory reset returns to unprovisioned */
    assert(provisioning_core_factory_reset() == 0);
    assert(provisioning_core_state() == PROV_UNPROVISIONED);

    /* re-provision after reset works */
    assert(provisioning_core_apply_key(k2) == 0);
    assert(provisioning_core_state() == PROV_PROVISIONED);
    assert(memcmp(provisioning_core_server_key(), k2, 32) == 0);

    /* reboot with persisted key boots straight to provisioned */
    reset_store(); memcpy(s_key, k, 32); s_prov = 1;
    assert(provisioning_core_init(&stub_store) == 0);
    assert(provisioning_core_state() == PROV_PROVISIONED);
    assert(memcmp(provisioning_core_server_key(), k, 32) == 0);

    printf("test_provisioning: OK\n");
    return 0;
}