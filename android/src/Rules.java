package io.github.vip3rousmango.couchchd.receiver;

import java.io.IOException;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.text.Normalizer;
import java.util.Locale;

/** Wire values shared with the sender; paths are names, never URIs or provider IDs. */
final class Rules {
    private static final char[] HEX = "0123456789abcdef".toCharArray();
    static MessageDigest sha() {
        try { return MessageDigest.getInstance("SHA-256"); }
        catch (NoSuchAlgorithmException e) { throw new AssertionError(e); }
    }
    static String hex(byte[] bytes) {
        char[] s = new char[bytes.length * 2];
        for (int i = 0; i < bytes.length; i++) {
            s[2*i] = HEX[(bytes[i] & 255) >> 4]; s[2*i+1] = HEX[bytes[i] & 15];
        }
        return new String(s);
    }
    static byte[] digestBytes(String s) throws IOException {
        if (s.length() != 64) throw new IOException("Invalid SHA-256 fingerprint.");
        byte[] bytes = new byte[32];
        for (int i = 0; i < bytes.length; i++) {
            int a = "0123456789abcdef".indexOf(s.charAt(2*i)), b = "0123456789abcdef".indexOf(s.charAt(2*i+1));
            if (a < 0 || b < 0) throw new IOException("Invalid SHA-256 fingerprint.");
            bytes[i] = (byte)((a << 4) | b);
        }
        return bytes;
    }
    static String code(byte[] pin, byte[] secret) {
        MessageDigest digest = sha(); digest.update(pin); String s = hex(digest.digest(secret));
        return s.substring(0,4) + "-" + s.substring(4,8) + "-" + s.substring(8,12);
    }
    // Display hints only. Use the same small ASCII alphabet in headers and on screen.
    static String deviceLabel(String raw) {
        if (raw == null) return "";
        StringBuilder label = new StringBuilder(48);
        boolean gap = false;
        for (int i = 0; i < raw.length() && label.length() < 48; i++) {
            char c = raw.charAt(i);
            if (c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || "._()-".indexOf(c) >= 0) {
                if (gap && label.length() > 0 && label.length() < 47) label.append(' ');
                label.append(c); gap = false;
            } else gap = true;
        }
        return label.toString();
    }
    static String fold(String name) {
        return Normalizer.normalize(name, Normalizer.Form.NFC).toUpperCase(Locale.ROOT).toLowerCase(Locale.ROOT);
    }
    static String[] parts(String path) throws IOException {
        if (path.length() > 4096) throw new IOException("Path too long.");
        String[] parts = path.split("/", -1);
        if (parts.length > 64) throw new IOException("Path too deep.");
        for (int i = 0; i < parts.length; i++) {
            String p = parts[i];
            if (p.isEmpty() || p.length() > 255 || !p.equals(p.trim()) || p.endsWith(".") ||
                    (p.startsWith(".") && !(p.equals(".chd_discs") && i < parts.length-1)))
                throw new IOException("Unsafe file or folder name.");
            for (int j = 0; j < p.length(); j++) {
                char c = p.charAt(j);
                if (Character.isISOControl(c) || Character.getType(c) == Character.FORMAT || "\\:*?\"<>|".indexOf(c) >= 0)
                    throw new IOException("Unsafe character in file or folder name.");
            }
        }
        return parts;
    }
    static String systemLabel(String s) throws IOException {
        switch (s) {
            case "psx": return "PlayStation"; case "dreamcast": return "Dreamcast"; case "psp": return "PSP";
            case "segacd": return "Sega CD"; case "pcenginecd": return "PC Engine CD"; case "neogeocd": return "Neo Geo CD";
            case "3do": return "3DO"; case "saturn": return "Saturn"; case "ps2": return "PlayStation 2";
            case "gc": return "GameCube"; case "wii": return "Wii";
            default: throw new IOException("Not an Android export platform: " + s);
        }
    }
    static boolean playlist(String s) { return s.toLowerCase(Locale.ROOT).endsWith(".m3u"); }
    static boolean supported(String system, String path) {
        String p = path.toLowerCase(Locale.ROOT);
        switch (system) {
            case "gc": case "wii": return p.endsWith(".rvz");
            case "psp": return p.endsWith(".chd") || p.endsWith(".cso");
            case "psx": case "dreamcast": case "segacd": case "pcenginecd": return p.endsWith(".chd") || playlist(p);
            case "neogeocd": case "3do": case "saturn": case "ps2": return p.endsWith(".chd");
            default: return false;
        }
    }
}
