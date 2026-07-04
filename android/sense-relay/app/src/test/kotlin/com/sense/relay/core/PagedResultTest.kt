package com.sense.relay.core

import com.sense.relay.core.model.PagedResult
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull
import kotlin.test.assertTrue

/**
 * PagedResult is the canonical "page of items, possibly more" container used
 * by every paged repository. These tests pin the constructor shape and the
 * "pure data, no Android imports" contract (the file compiles as a JVM unit
 * test, which fails fast if anyone pulls in an Android-only type).
 */
class PagedResultTest {

    @Test fun loadingIsConstructable() {
        val r: PagedResult<Int> = PagedResult.Loading
        assertTrue(r is PagedResult.Loading)
    }

    @Test fun pageCarriesItemsAndCursor() {
        val page = PagedResult.Page(items = listOf("a", "b"), nextCursor = "cursor-1")
        assertEquals(listOf("a", "b"), page.items)
        assertEquals("cursor-1", page.nextCursor)
    }

    @Test fun pageAllowsNullCursor() {
        // A page with no cursor is also valid: a single-shot server response.
        val page = PagedResult.Page(items = listOf(1, 2), nextCursor = null)
        assertNull(page.nextCursor)
    }

    @Test fun exhaustedIsConstructable() {
        val r: PagedResult<Int> = PagedResult.Exhausted
        assertTrue(r is PagedResult.Exhausted)
    }

    @Test fun errorCarriesCause() {
        val cause = RuntimeException("boom")
        val r = PagedResult.Error(cause)
        assertEquals(cause, r.cause)
    }

    @Test fun isGeneric() {
        // Compile-time check: PagedResult is usable over arbitrary T.
        val s: PagedResult<String> = PagedResult.Loading
        val i: PagedResult<Int> = PagedResult.Exhausted
        val u: PagedResult<Unit> = PagedResult.Page(emptyList(), null)
        assertTrue(s is PagedResult.Loading)
        assertTrue(i is PagedResult.Exhausted)
        assertTrue(u is PagedResult.Page<*>)
    }
}
