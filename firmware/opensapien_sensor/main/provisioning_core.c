/* firmware/opensapien_sensor/main/provisioning_core.c */
#include "provisioning_core.h"
#include <string.h>

static const prov_store_t *s_store;
static prov_state_t s_state;
static uint8_t s_key[32];

int provisioning_core_init(const prov_store_t *store) {
    s_store = store;
    uint8_t provisioned = 0;
    if (store->get(s_key, &provisioned) == 0 && provisioned) {
        s_state = PROV_PROVISIONED;
    } else {
        s_state = PROV_UNPROVISIONED;
        memset(s_key, 0, sizeof s_key);
    }
    return 0;
}

prov_state_t provisioning_core_state(void) { return s_state; }

int provisioning_core_apply_key(const uint8_t key[32]) {
    if (s_state == PROV_PROVISIONED) return -1;
    if (s_store->set(key) != 0) return -1;       /* persist first */
    memcpy(s_key, key, 32);
    s_state = PROV_PROVISIONED;
    return 0;
}

int provisioning_core_factory_reset(void) {
    if (s_store->clear() != 0) return -1;
    memset(s_key, 0, sizeof s_key);
    s_state = PROV_UNPROVISIONED;
    return 0;
}

const uint8_t *provisioning_core_server_key(void) { return s_key; }