package com.aiblackbox.portal.data.remote

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class TailnetAddressTest {

    @Test fun cgnat_range_classification() {
        assertTrue(TailnetAddress.isCgnatIpv4("100.64.0.1"))
        assertTrue(TailnetAddress.isCgnatIpv4("100.88.0.7"))
        assertTrue(TailnetAddress.isCgnatIpv4("100.127.255.255"))
        assertFalse(TailnetAddress.isCgnatIpv4("100.63.255.255"))   // just below
        assertFalse(TailnetAddress.isCgnatIpv4("100.128.0.1"))      // just above
        assertFalse(TailnetAddress.isCgnatIpv4("192.168.1.5"))      // LAN
        assertFalse(TailnetAddress.isCgnatIpv4("10.0.0.1"))         // private
        assertFalse(TailnetAddress.isCgnatIpv4("100.64.0"))         // too few octets
        assertFalse(TailnetAddress.isCgnatIpv4("not.an.ip.addr"))
        assertFalse(TailnetAddress.isCgnatIpv4(""))
    }
}
