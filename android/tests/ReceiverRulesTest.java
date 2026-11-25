package io.github.vip3rousmango.couchchd.receiver;

import java.io.IOException;
import java.util.Arrays;

/** Runs with just a JDK: compile alongside src/Rules.java, then run this main class. */
public final class ReceiverRulesTest {
    static void require(boolean truth) { if (!truth) throw new AssertionError(); }
    static void invalid(String path) throws IOException {
        try { Rules.parts(path); } catch (IOException good) { return; }
        throw new AssertionError("accepted path: " + path);
    }
    public static void main(String[] args) throws Exception {
        byte[] empty = new byte[32]; require(Rules.code(empty,empty).equals("f5a5-fd42-d16a"));
        byte[] nonzero = new byte[32]; Arrays.fill(nonzero,(byte)0xa5);
        require(Arrays.equals(nonzero,Rules.digestBytes(Rules.hex(nonzero))));
        require(!Rules.code(empty,nonzero).equals(Rules.code(nonzero,empty)));
        try { Rules.digestBytes(Rules.hex(nonzero).toUpperCase(java.util.Locale.ROOT)); throw new AssertionError("accepted uppercase pin"); }
        catch (IOException good) { }
        require(Rules.parts("Sets/.chd_discs/Café (Disc 1).chd").length == 3);
        for (String p : new String[]{"", "/disc.chd", "a//b.chd", "a/../b.chd", "a/./b.chd", "a/", "C:/b.chd", "a\\b.chd", ".hidden/x.chd", ".chd_discs", "a\n.chd", "a\u202eb.chd", "a./b.chd"}) invalid(p);
        require(Rules.fold("Café").equals(Rules.fold("CAFE\u0301")));
        require(Rules.supported("psx","set.M3U") && Rules.supported("psp","test.cso") && Rules.supported("gc","test.rvz"));
        require(!Rules.supported("psx","test.cso") && !Rules.supported("psp","set.m3u") && !Rules.supported("other","test.chd"));
    }
}
