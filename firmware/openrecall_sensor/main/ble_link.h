/*
 * BLE link — NimBLE GATT peripheral bridging the device to the phone relay.
 *
 * Three characteristics on one service (separated planes):
 *   - AUDIO   (notify): §C.6 packets, device -> phone
 *   - COMMAND (write) : signed §D commands, phone -> device
 *   - ACK     (notify): command acks / status, device -> phone
 *
 * The phone relay forwards these to/from the WebSocket server; the device never
 * speaks §E. Runs on core 0 (the radio core), opposite the core-1 audio task.
 */
#pragma once

#include "esp_err.h"

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// Called (on the NimBLE host task) when a signed §D command is written by the phone.
typedef void (*ble_command_handler_t)(const uint8_t *data, size_t len);

// Initialise NimBLE, register the GATT service, and start advertising as "OpenRecall".
esp_err_t ble_link_start(ble_command_handler_t on_command);

// Suspend BLE for the bounded WiFi transfer window (spec §3.2): stop advertising
// + disconnect the phone. Resume re-advertises. The phone is busy on WiFi anyway.
void ble_link_suspend(void);
void ble_link_resume(void);

// Notify a §C.6 audio packet to the subscribed phone. Returns 0 on success, <0 if
// not connected/subscribed or on error. Non-blocking; drops if the link is down.
int ble_link_notify_audio(const uint8_t *data, size_t len);

// Notify a command ack / status frame to the phone.
int ble_link_notify_ack(const uint8_t *data, size_t len);

// True while a phone is connected.
bool ble_link_connected(void);

// The negotiated ATT MTU for the current connection, or 0 if not connected /
// MTU not yet exchanged. A §C.6 packet handed to [ble_link_notify_audio] must
// fit in (mtu - 3) bytes — ATT notifications are a single PDU and don't
// fragment, so a larger buffer is silently truncated to the first mtu-3 bytes.
// The drainer queries this to size each packet.
uint16_t ble_link_att_mtu(void);

#ifdef __cplusplus
}
#endif
