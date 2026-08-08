package com.opensapien.relay.core.model

/**
 * A page of items in a paged, cursor-based list. The "first emit is Loading,
 * subsequent emits are Page/Exhausted/Error" pattern is what every paged
 * repository produces. Generic on the item type T; pure data (no Android
 * imports), so it's safe to share with tests and other modules.
 */
sealed interface PagedResult<out T> {
    /** Initial state — the first page is in flight. */
    data object Loading : PagedResult<Nothing>

    /**
     * A page of items, plus the cursor to fetch the next page (null when the
     * server doesn't expose one for this response). An empty list with a
     * non-null cursor is valid; an empty list with a null cursor is the
     * server's "nothing to return" signal.
     */
    data class Page<T>(
        val items: List<T>,
        val nextCursor: String?,
    ) : PagedResult<T>

    /** No more pages available. Distinct from [Page] with an empty list so
     *  the UI can stop showing load-more affordances. */
    data object Exhausted : PagedResult<Nothing>

    /** The page request failed. UI should surface a retry. */
    data class Error(val cause: Throwable) : PagedResult<Nothing>
}
