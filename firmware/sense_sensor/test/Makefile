# Host-side contract tests for dependency-free firmware modules (no ESP toolchain).
# Run:  make
CC ?= cc
CFLAGS ?= -std=c11 -Wall -Wextra -I../main

test: c6 vad dsp provisioning

c6: /tmp/c6test
	/tmp/c6test

vad: /tmp/vadtest
	/tmp/vadtest

dsp: /tmp/dsptest
	/tmp/dsptest

provisioning: /tmp/provtest
	/tmp/provtest

/tmp/c6test: test_c6_packet.c ../main/c6_packet.c ../main/c6_packet.h ../main/config.h
	$(CC) $(CFLAGS) ../main/c6_packet.c test_c6_packet.c -o $@

/tmp/vadtest: test_vad.c ../main/vad.c ../main/vad.h ../main/config.h
	$(CC) $(CFLAGS) ../main/vad.c test_vad.c -o $@

/tmp/provtest: test_provisioning.c ../main/provisioning_core.c ../main/provisioning_core.h
	$(CC) $(CFLAGS) ../main/provisioning_core.c test_provisioning.c -o $@

/tmp/dsptest: test_mic_dsp.c ../main/mic_dsp.c ../main/mic_dsp.h ../main/config.h
	$(CC) $(CFLAGS) ../main/mic_dsp.c test_mic_dsp.c -o $@

clean:
	rm -f /tmp/c6test /tmp/vadtest /tmp/dsptest /tmp/provtest

.PHONY: test c6 vad dsp provisioning clean