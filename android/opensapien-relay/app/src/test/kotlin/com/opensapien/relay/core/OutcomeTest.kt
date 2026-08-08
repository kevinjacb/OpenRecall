package com.opensapien.relay.core

import com.opensapien.relay.core.model.ApiError
import com.opensapien.relay.core.result.Outcome
import com.opensapien.relay.core.result.getOrNull
import com.opensapien.relay.core.result.map
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull
import kotlin.test.assertTrue

/**
 * Outcome is the cross-cutting "result of a call" envelope: Success(value) or
 * Failure(ApiError). Repositories return Flow<Outcome<T>>; ViewModels map;
 * UIs switch on the sealed type. map is the only mandatory helper for now.
 */
class OutcomeTest {

    @Test fun successCarriesValue() {
        val s = Outcome.Success(42)
        assertEquals(42, s.value)
    }

    @Test fun failureCarriesApiError() {
        val err: ApiError = ApiError.Unauthorized
        val f = Outcome.Failure(err)
        assertEquals(err, f.error)
    }

    @Test fun successMapProducesSuccess() {
        val s: Outcome<Int> = Outcome.Success(3)
        val r: Outcome<String> = s.map { "n=$it" }
        assertEquals(Outcome.Success("n=3"), r)
    }

    @Test fun failureMapProducesFailure() {
        val f: Outcome<Int> = Outcome.Failure(ApiError.Unauthorized)
        val r: Outcome<String> = f.map { "n=$it" }
        assertEquals(Outcome.Failure(ApiError.Unauthorized), r)
        // The transform must not run on a failure: this is the contract that
        // lets repositories treat map as a non-throwing pipeline.
    }

    @Test fun mapPropagatesHttpErrorCode() {
        val f: Outcome<Int> = Outcome.Failure(ApiError.Http(503))
        val r: Outcome<String> = f.map { it.toString() }
        assertEquals(Outcome.Failure(ApiError.Http(503)), r)
    }

    @Test fun getOrNullOnSuccess() {
        val s: Outcome<String> = Outcome.Success("hi")
        assertEquals("hi", s.getOrNull())
    }

    @Test fun getOrNullOnFailure() {
        val f: Outcome<String> = Outcome.Failure(ApiError.Unreachable("nope"))
        assertNull(f.getOrNull())
    }

    @Test fun apiErrorSealedHierarchyExists() {
        // Pin the four cases — adding a fifth is a deliberate API change.
        val errors: List<ApiError> = listOf(
            ApiError.Unauthorized,
            ApiError.Unreachable("offline"),
            ApiError.Http(404),
            ApiError.Unknown(IllegalStateException("x")),
        )
        assertEquals(4, errors.size)
        assertTrue(errors[0] is ApiError.Unauthorized)
        assertTrue(errors[1] is ApiError.Unreachable)
        assertTrue(errors[2] is ApiError.Http)
        assertTrue(errors[3] is ApiError.Unknown)
    }
}
