/* firmware/sense_sensor/main/provisioning_core.h */
#ifndef PROVISIONING_CORE_H
#define PROVISIONING_CORE_H
#include <stdint.h>

typedef enum { PROV_UNPROVISIONED = 0, PROV_PROVISIONED = 1 } prov_state_t;

typedef struct {
    int (*get)(uint8_t out_key[32], uint8_t *out_provisioned);
    int (*set)(const uint8_t key[32]);
    int (*clear)(void);
} prov_store_t;

int provisioning_core_init(const prov_store_t *store);
prov_state_t provisioning_core_state(void);
int provisioning_core_apply_key(const uint8_t key[32]);
int provisioning_core_factory_reset(void);
const uint8_t *provisioning_core_server_key(void);

#endif