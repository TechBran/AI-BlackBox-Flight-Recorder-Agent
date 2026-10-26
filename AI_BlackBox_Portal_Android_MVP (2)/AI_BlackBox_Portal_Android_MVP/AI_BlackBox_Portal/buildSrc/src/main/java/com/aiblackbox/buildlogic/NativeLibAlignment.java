package com.aiblackbox.buildlogic;

import java.io.IOException;
import java.io.InputStream;
import java.io.RandomAccessFile;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.zip.ZipEntry;
import java.util.zip.ZipFile;

/**
 * Checks that every native library in an APK can be loaded on a 16 KB-page device
 * (Android 15+ hardware with 16 KB pages, and Android 16's page-size compat warning).
 *
 * <p>Two things must hold, the same two that the AOSP check_elf_alignment.sh and
 * {@code zipalign -c -P 16} check:
 * <ul>
 *   <li>Every PT_LOAD segment of a 64-bit library is aligned to at least the page size. A
 *       library linked with the old 4 KB max-page-size (NDK r27 and older by default) fails
 *       to dlopen on a 16 KB kernel. 32-bit libraries are skipped: 16 KB pages exist only
 *       on 64-bit devices.</li>
 *   <li>A library stored uncompressed (AGP's default for minSdk 23+, mmapped straight from
 *       the APK) starts at an APK offset that is a multiple of the page size.</li>
 * </ul>
 *
 * <p>It fails closed: a library it cannot parse is reported, never waved through. Only
 * {@code lib/<abi>/*.so} entries count, because those are the only ones the installer and
 * {@code System.loadLibrary} use.
 */
public final class NativeLibAlignment {

    /** The page size Android's 16 KB devices use, and the alignment AOSP requires. */
    public static final int PAGE_SIZE_16K = 16 * 1024;

    // Program headers sit right after the ELF header in every real library. Anything that
    // needs more than this to reach them is malformed, and is not read into memory.
    private static final int MAX_HEADER_BYTES = 1 << 20;

    private static final int PT_LOAD = 1;
    private static final int LOCAL_HEADER_SIG = 0x04034b50;
    private static final int CENTRAL_HEADER_SIG = 0x02014b50;
    private static final int END_OF_CENTRAL_DIR_SIG = 0x06054b50;

    private NativeLibAlignment() {}

    /**
     * Returns one human-readable problem per offending library in {@code apk}, or an empty
     * list when every native library is safe on {@code pageSize}-byte pages.
     */
    public static List<String> checkApk(Path apk, int pageSize) throws IOException {
        Map<String, Long> storedOffsets;
        try {
            storedOffsets = storedEntryDataOffsets(apk);
        } catch (MalformedException e) {
            return List.of(apk.getFileName() + ": not a readable APK (" + e.getMessage() + ")");
        }

        List<String> problems = new ArrayList<>();
        try (ZipFile zip = new ZipFile(apk.toFile())) {
            List<String> names = new ArrayList<>();
            zip.stream().map(ZipEntry::getName).filter(NativeLibAlignment::isNativeLibrary).forEach(names::add);
            Collections.sort(names);
            for (String name : names) {
                ZipEntry entry = zip.getEntry(name);
                String elfProblem;
                try (InputStream in = zip.getInputStream(entry)) {
                    elfProblem = checkElf(in, pageSize);
                }
                if (elfProblem != null) {
                    problems.add(name + ": " + elfProblem);
                }
                Long offset = storedOffsets.get(name);
                if (offset != null && offset % pageSize != 0) {
                    problems.add(name + ": stored uncompressed at APK offset " + offset
                        + ", which is not a multiple of " + pageSize
                        + ", so it cannot be mapped from the APK on " + (pageSize / 1024)
                        + " KB-page devices (zipalign -P " + (pageSize / 1024) + ")");
                }
            }
        }
        return problems;
    }

    /** {@code lib/<abi>/<name>.so}: the entries the package installer extracts or maps. */
    static boolean isNativeLibrary(String name) {
        String[] parts = name.split("/", -1);
        return parts.length == 3 && parts[0].equals("lib") && !parts[1].isEmpty()
            && parts[2].endsWith(".so") && parts[2].length() > 3;
    }

    /**
     * Reads an ELF image's header and program headers from {@code in} and returns null if
     * every PT_LOAD segment is aligned to at least {@code pageSize} (or the image is 32-bit),
     * otherwise why it is not.
     */
    static String checkElf(InputStream in, int pageSize) throws IOException {
        byte[] head = in.readNBytes(64);
        if (head.length < 52 || head[0] != 0x7f || head[1] != 'E' || head[2] != 'L' || head[3] != 'F') {
            return "not an ELF shared object";
        }
        int elfClass = head[4];
        if (elfClass == 1) {
            return null; // ELF32: 32-bit ABIs never run with 16 KB pages
        }
        if (elfClass != 2) {
            return "unknown ELF class " + elfClass;
        }
        if (head.length < 64) {
            return "truncated ELF header";
        }
        ByteOrder order;
        if (head[5] == 1) {
            order = ByteOrder.LITTLE_ENDIAN;
        } else if (head[5] == 2) {
            order = ByteOrder.BIG_ENDIAN;
        } else {
            return "unknown ELF byte order " + head[5];
        }
        ByteBuffer header = ByteBuffer.wrap(head).order(order);
        long phoff = header.getLong(0x20);
        int phentsize = Short.toUnsignedInt(header.getShort(0x36));
        int phnum = Short.toUnsignedInt(header.getShort(0x38));
        if (phnum == 0xffff) {
            return "uses extended program-header numbering (PN_XNUM), which this check cannot read";
        }
        if (phnum == 0) {
            return "has no program headers";
        }
        if (phentsize < 56) {
            return "program-header entries of " + phentsize + " bytes are too small for ELF64";
        }
        long end = phoff + (long) phentsize * phnum;
        if (phoff < 64 || phoff > MAX_HEADER_BYTES || end > MAX_HEADER_BYTES) {
            return "program headers at offset " + phoff + " are outside the readable header area";
        }
        byte[] rest = in.readNBytes((int) (end - 64));
        if (rest.length < end - 64) {
            return "truncated: the program headers end past the end of the file";
        }
        ByteBuffer image = ByteBuffer.allocate((int) end).order(order).put(head).put(rest);

        int loads = 0;
        List<String> misaligned = new ArrayList<>();
        for (int i = 0; i < phnum; i++) {
            int base = (int) (phoff + (long) i * phentsize);
            if (image.getInt(base) != PT_LOAD) {
                continue;
            }
            loads++;
            long align = image.getLong(base + 0x30);
            if (Long.compareUnsigned(align, pageSize) < 0) {
                misaligned.add("LOAD segment " + i + " is aligned to 0x" + Long.toHexString(align));
            }
        }
        if (loads == 0) {
            return "has no PT_LOAD segment";
        }
        if (!misaligned.isEmpty()) {
            return String.join(", ", misaligned) + "; " + (pageSize / 1024)
                + " KB-page devices need 0x" + Integer.toHexString(pageSize)
                + " or more (link with -Wl,-z,max-page-size=" + pageSize + ", NDK r28+ does by default)";
        }
        return null;
    }

    /**
     * Maps the name of every STORED (uncompressed) {@code lib/} entry to the APK offset where
     * its data starts, from the central directory and each entry's local header (whose extra
     * field, which zipalign pads, may differ from the central copy).
     */
    private static Map<String, Long> storedEntryDataOffsets(Path apk) throws IOException, MalformedException {
        Map<String, Long> offsets = new HashMap<>();
        try (RandomAccessFile file = new RandomAccessFile(apk.toFile(), "r")) {
            long length = file.length();
            if (length < 22) {
                throw new MalformedException("too short for a zip");
            }
            int tailLength = (int) Math.min(length, 22 + 0xffff);
            byte[] tail = new byte[tailLength];
            file.seek(length - tailLength);
            file.readFully(tail);
            ByteBuffer t = ByteBuffer.wrap(tail).order(ByteOrder.LITTLE_ENDIAN);
            int eocd = -1;
            for (int i = tailLength - 22; i >= 0; i--) {
                if (t.getInt(i) == END_OF_CENTRAL_DIR_SIG) {
                    eocd = i;
                    break;
                }
            }
            if (eocd < 0) {
                throw new MalformedException("no end-of-central-directory record");
            }
            int count = Short.toUnsignedInt(t.getShort(eocd + 10));
            long cdSize = Integer.toUnsignedLong(t.getInt(eocd + 12));
            long cdOffset = Integer.toUnsignedLong(t.getInt(eocd + 16));
            if (count == 0xffff || cdSize == 0xffffffffL || cdOffset == 0xffffffffL) {
                throw new MalformedException("zip64 archives are not supported by this check");
            }
            if (cdOffset + cdSize > length || cdSize > Integer.MAX_VALUE) {
                throw new MalformedException("central directory outside the file");
            }
            byte[] cdBytes = new byte[(int) cdSize];
            file.seek(cdOffset);
            file.readFully(cdBytes);
            ByteBuffer cd = ByteBuffer.wrap(cdBytes).order(ByteOrder.LITTLE_ENDIAN);
            int pos = 0;
            for (int n = 0; n < count; n++) {
                if (pos + 46 > cdBytes.length || cd.getInt(pos) != CENTRAL_HEADER_SIG) {
                    throw new MalformedException("bad central-directory entry " + n);
                }
                int method = Short.toUnsignedInt(cd.getShort(pos + 10));
                int nameLength = Short.toUnsignedInt(cd.getShort(pos + 28));
                int extraLength = Short.toUnsignedInt(cd.getShort(pos + 30));
                int commentLength = Short.toUnsignedInt(cd.getShort(pos + 32));
                long localOffset = Integer.toUnsignedLong(cd.getInt(pos + 42));
                if (pos + 46 + nameLength > cdBytes.length) {
                    throw new MalformedException("bad central-directory entry " + n);
                }
                String name = new String(cdBytes, pos + 46, nameLength, java.nio.charset.StandardCharsets.UTF_8);
                if (method == ZipEntry.STORED && isNativeLibrary(name)) {
                    byte[] local = new byte[30];
                    if (localOffset + 30 > length) {
                        throw new MalformedException("local header of " + name + " outside the file");
                    }
                    file.seek(localOffset);
                    file.readFully(local);
                    ByteBuffer l = ByteBuffer.wrap(local).order(ByteOrder.LITTLE_ENDIAN);
                    if (l.getInt(0) != LOCAL_HEADER_SIG) {
                        throw new MalformedException("bad local header for " + name);
                    }
                    long dataOffset = localOffset + 30
                        + Short.toUnsignedInt(l.getShort(26)) + Short.toUnsignedInt(l.getShort(28));
                    offsets.put(name, dataOffset);
                }
                pos += 46 + nameLength + extraLength + commentLength;
            }
        }
        return offsets;
    }

    private static final class MalformedException extends Exception {
        MalformedException(String message) {
            super(message);
        }
    }
}
