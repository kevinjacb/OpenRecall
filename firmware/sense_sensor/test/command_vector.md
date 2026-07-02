# §D command test vector (server → device)

A real signed command produced by the server (`CommandSigner`), for verifying the
firmware's `commands` module on the bench. The BLE command-characteristic write is
`[64-byte raw Ed25519 signature] || [canonical payload JSON]`.

## Provision the server key

Put this into `main/config.h` `SERVER_ED25519_PUBKEY` (32 bytes):

```
03a2a60b3898ff41aabdca6785c5618d2faf5dc5b7953069435a73cc33522810
```

## Frames (213 bytes each)

**VALID** — device must verify, execute `capture_photo`, and ack `demo:0`:

```
beed6b060a7d35c3d651dcba1afcfc9fc2a5531be06226472ba58c074748a301
6847cddd4a065a09957bb2749590e5c62a9ac4b57e3c23c6c8ec95e7e7c21c02
7b22636f6d6d616e645f6964223a2264656d6f3a30222c22657870697265735f
6174223a22323032362d30372d30315431323a30353a30305a222c2269737375
65645f6174223a22323032362d30372d30315431323a30303a30305a222c2270
6172616d73223a7b7d2c2273657373696f6e5f6964223a2264656d6f222c2274
797065223a22636170747572655f70686f746f227d
```

**FORGED** — one signature bit flipped; device must **drop** it (no ack, no exec):

```
bfed6b060a7d35c3d651dcba1afcfc9fc2a5531be06226472ba58c074748a301
6847cddd4a065a09957bb2749590e5c62a9ac4b57e3c23c6c8ec95e7e7c21c02
7b22636f6d6d616e645f6964223a2264656d6f3a30222c22657870697265735f
6174223a22323032362d30372d30315431323a30353a30305a222c2269737375
65645f6174223a22323032362d30372d30315431323a30303a30305a222c2270
6172616d73223a7b7d2c2273657373696f6e5f6964223a2264656d6f222c2274
797065223a22636170747572655f70686f746f227d
```

Payload JSON (what the signature covers):

```json
{"command_id":"demo:0","expires_at":"2026-07-01T12:05:00Z","issued_at":"2026-07-01T12:00:00Z","params":{},"session_id":"demo","type":"capture_photo"}
```

Regenerate with: `server/` → the snippet in the firmware commit that created this file.
