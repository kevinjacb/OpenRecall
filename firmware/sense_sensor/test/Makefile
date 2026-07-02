# Host-side contract tests for dependency-free firmware modules (no ESP toolchain).
# Run:  make
CC ?= cc
CFLAGS ?= -std=c11 -Wall -Wextra -I../main

test: c6 vad

c6: /tmp/c6test
	/tmp/c6test

vad: /tmp/vadtest
	/tmp/vadtest

/tmp/c6test: test_c6_packet.c ../main/c6_packet.c ../main/c6_packet.h ../main/config.h
	$(CC) $(CFLAGS) ../main/c6_packet.c test_c6_packet.c -o $@

/tmp/vadtest: test_vad.c ../main/vad.c ../main/vad.h ../main/config.h
	$(CC) $(CFLAGS) ../main/vad.c test_vad.c -o $@

clean:
	rm -f /tmp/c6test /tmp/vadtest

.PHONY: test c6 vad clean
