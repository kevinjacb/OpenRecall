/*
 * Device command handling (§D) — verify, dedupe, execute, ack.
 *
 * The phone writes, to the BLE command characteristic, a frame of:
 *     [64-byte raw Ed25519 signature][canonical payload JSON]
 * The device verifies the signature over the payload bytes against the provisioned
 * server public key (libsodium; mbedTLS here has no Ed25519), parses the command,
 * executes it at most once per command_id, and acks with the command_id.
 *
 * Wall-clock expiry is enforced upstream by the phone relay (which has synced time);
 * the device — which may have no clock — enforces idempotency via command_id dedupe.
 */
#pragma once

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// Sink for the ack frame (the command_id bytes); the phone wraps it as §E command_ack.
typedef int (*command_ack_fn)(const uint8_t *data, size_t len);

// Initialise libsodium and store the server's 32-byte Ed25519 public key + ack sink.
// Returns 0 on success, <0 if libsodium fails to init.
int commands_init(const uint8_t server_pubkey[32], command_ack_fn ack);

/* Update the trusted server public key at runtime (after provisioning). */
void commands_set_pubkey(const uint8_t server_pubkey[32]);

// Handle one BLE command write: sig(64) || payload JSON. Verifies, dedupes,
// executes, and acks. Drops (no ack) anything that fails verification or parsing.
void commands_handle(const uint8_t *data, size_t len);

#ifdef __cplusplus
}
#endif
