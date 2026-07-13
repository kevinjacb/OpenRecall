package com.sense.relay.data

import com.sense.relay.http.ErrorCode
import com.sense.relay.http.HttpApiError
import com.sense.relay.http.dto.CommandRecordDto
import com.sense.relay.http.dto.DtoJson
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

class CommandRepositoryTest {

    @Test
    fun `listActive maps DTOs to domain Commands`() = runTest {
        val repo = CommandRepository(StubCommandApi(
            listResponse = """[{"command_id":"c1","session_id":"s1","type":"capture_photo","params":{},"issued_at":"2026-07-01T00:00:00Z","expires_at":"2026-07-01T00:30:00Z","status":"PENDING","history":[]}]""",
        ))
        val result = repo.listActive()
        assertEquals(1, result.size)
        assertEquals("c1", result[0].commandId)
        assertEquals(CommandStatus.PENDING, result[0].status)
        assertEquals(0, result[0].history.size)
    }

    @Test
    fun `listActive returns empty on 200 with empty list`() = runTest {
        val repo = CommandRepository(StubCommandApi(listResponse = "[]"))
        assertEquals(0, repo.listActive().size)
    }

    @Test
    fun `ack transitions command from DELIVERED to EXECUTING`() = runTest {
        val repo = CommandRepository(StubCommandApi(
            ackResponse = """{"command_id":"c1","session_id":"s1","type":"capture_photo","params":{},"issued_at":"2026-07-01T00:00:00Z","expires_at":"2026-07-01T00:30:00Z","status":"EXECUTING","history":[]}""",
        ))
        val result = repo.ack("c1")
        assertEquals("c1", result.commandId)
        assertEquals(CommandStatus.EXECUTING, result.status)
    }

    @Test
    fun `ack on terminal command becomes error`() = runTest {
        val repo = CommandRepository(StubCommandApi(
            ackResponse = """{"code":"conflict","message":"command 'c1' is in status 'COMPLETED'"}""",
            ackStatus = 409,
        ))
        try {
            repo.ack("c1")
            fail("expected error")
        } catch (e: HttpApiError) {
            assertTrue(e.message!!.contains("COMPLETED"))
        }
    }

    @Test
    fun `history preserves order from DTO`() = runTest {
        val repo = CommandRepository(StubCommandApi(
            getResponse = """{"command_id":"c1","session_id":"s1","type":"capture_photo","params":{},"issued_at":"2026-07-01T00:00:00Z","expires_at":"2026-07-01T00:30:00Z","status":"EXECUTING","history":[
                {"from_status":"PENDING","to_status":"VALIDATED","at":"2026-07-01T00:00:01Z","detail":{"event":"validate"}},
                {"from_status":"VALIDATED","to_status":"ISSUED","at":"2026-07-01T00:00:02Z","detail":{"event":"issue"}},
                {"from_status":"ISSUED","to_status":"DELIVERED","at":"2026-07-01T00:00:03Z","detail":{}},
                {"from_status":"DELIVERED","to_status":"EXECUTING","at":"2026-07-01T00:00:04Z","detail":{}}
            ]}""",
            getStatus = 200,
        ))
        val cmd = repo.get("c1")
        assertEquals(4, cmd.history.size)
        assertEquals(CommandStatus.PENDING, cmd.history[0].fromStatus)
        assertEquals(CommandStatus.VALIDATED, cmd.history[0].toStatus)
        assertEquals(CommandStatus.EXECUTING, cmd.history[3].toStatus)
    }
}
