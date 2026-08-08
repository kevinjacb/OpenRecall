package com.openrecall.relay.relay

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

/**
 * Pins [Backoff]'s exponential schedule and the give-up cap — pure, no Android,
 * no I/O. The sequence is the contract [RelayService]'s auto-reconnect follows:
 * 1s → 2s → 4s → 8s → 30s (cap), giving up after [Backoff.MAX_ATTEMPTS].
 */
class BackoffTest {

    @Test fun backoffSequenceIsExponentialThenCapped() {
        assertEquals(1_000L, Backoff.nextBackoffMs(0), "first retry waits 1s")
        assertEquals(2_000L, Backoff.nextBackoffMs(1))
        assertEquals(4_000L, Backoff.nextBackoffMs(2))
        assertEquals(8_000L, Backoff.nextBackoffMs(3))
        assertEquals(30_000L, Backoff.nextBackoffMs(4), "steps into the 30s cap")
        assertEquals(30_000L, Backoff.nextBackoffMs(5))
        assertEquals(30_000L, Backoff.nextBackoffMs(99), "clamps to the 30s cap past the last step")
    }

    @Test fun negativeAttemptIsTreatedAsZero() {
        assertEquals(1_000L, Backoff.nextBackoffMs(-1))
    }

    @Test fun givesUpAfterMaxAttempts() {
        // The cap is what flips the UI to terminal Failed — assert the boundary.
        assertFalse(Backoff.shouldGiveUp(0), "first attempt does not give up")
        assertFalse(Backoff.shouldGiveUp(Backoff.MAX_ATTEMPTS - 1), "last allowed attempt does not give up")
        assertTrue(Backoff.shouldGiveUp(Backoff.MAX_ATTEMPTS), "gives up at the cap")
        assertTrue(Backoff.shouldGiveUp(99), "stays given up past the cap")
    }

    @Test fun reconnectingStateCarriesTheBackoffDelay() {
        // The Reconnecting enum value surfaces the wait to the UI; assert it
        // carries exactly the scheduled delay so the Device/Home "Reconnecting
        // (after Nms)" label matches the actual wait.
        assertEquals(Backoff.nextBackoffMs(0), RelayConnectionState.Reconnecting(Backoff.nextBackoffMs(0)).afterMs)
        assertEquals(Backoff.nextBackoffMs(4), RelayConnectionState.Reconnecting(Backoff.nextBackoffMs(4)).afterMs)
    }
}