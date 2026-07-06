package com.sense.relay.data

import com.sense.relay.domain.model.SessionId
import com.sense.relay.http.dto.CaptureEventDto
import com.sense.relay.http.dto.SessionDetailsDto
import com.sense.relay.http.dto.SessionsPageDto

/**
 * Narrow seam over the three session endpoints [SessionRepositoryImpl]
 * needs. Exists because [com.sense.relay.http.SenseHttpClient] is `final`
 * and does real OkHttp I/O, so it cannot be faked directly in a host unit
 * test — the repository depends on this interface and tests supply a fake
 * (the same testability pattern as Phase 4's `fetch` seam for the status
 * poller). The production impl (`HttpSessionApi` in `RepositoryModule`) is
 * config-aware: it resolves the current client per call.
 *
 * The methods return wire DTOs; the repository owns the DTO → domain
 * mapping so the mapping stays testable and total.
 */
interface SessionApi {
    suspend fun listSessions(limit: Int, cursor: String?): SessionsPageDto
    suspend fun getSession(id: SessionId): SessionDetailsDto
    suspend fun getSessionEvents(id: SessionId): List<CaptureEventDto>
}
