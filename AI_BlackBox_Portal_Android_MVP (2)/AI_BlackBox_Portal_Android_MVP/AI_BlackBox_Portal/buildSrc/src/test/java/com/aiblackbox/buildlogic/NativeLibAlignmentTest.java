package com.aiblackbox.buildlogic;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertThrows;
import static org.junit.Assert.assertTrue;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Arrays;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.zip.CRC32;
import java.util.zip.ZipEntry;
import java.util.zip.ZipOutputStream;
import org.gradle.api.GradleException;
import org.gradle.api.Project;
import org.gradle.testfixtures.ProjectBuilder;
import org.junit.Rule;
import org.junit.Test;
import org.junit.rules.TemporaryFolder;

/**
 * The page-alignment gate the APK must pass before it can load on a 16 KB-page device
 * (audit A01-activities-root-11). Every fixture is synthesized here: no SDK, no network.
 */
public class NativeLibAlignmentTest {

    private static final int PAGE = NativeLibAlignment.PAGE_SIZE_16K;
    private static final int ALIGN_4K = 0x1000;
    private static final int ALIGN_16K = 0x4000;
    private static final int ALIGN_64K = 0x10000;

    @Rule public TemporaryFolder tmp = new TemporaryFolder();

    // ---- ELF program headers -------------------------------------------------------------

    @Test
    public void a64BitLibraryWith4KbLoadSegmentsIsRejected() throws IOException {
        // What termux terminal-emulator v0.118.0 ships: NDK r21, max-page-size 4096.
        Path apk = apk(deflated("lib/arm64-v8a/libtermux.so", elf64(ALIGN_4K, ALIGN_4K)));

        List<String> problems = NativeLibAlignment.checkApk(apk, PAGE);

        assertEquals(problems.toString(), 1, problems.size());
        assertTrue(problems.get(0), problems.get(0).contains("lib/arm64-v8a/libtermux.so"));
        assertTrue(problems.get(0), problems.get(0).contains("0x1000"));
    }

    @Test
    public void a64BitLibraryWith16KbLoadSegmentsPasses() throws IOException {
        Path apk = apk(deflated("lib/arm64-v8a/libLiteRt.so", elf64(ALIGN_16K, ALIGN_16K)));

        assertEquals(List.of(), NativeLibAlignment.checkApk(apk, PAGE));
    }

    @Test
    public void alignmentAbove16KbAlsoPasses() throws IOException {
        Path apk = apk(deflated("lib/x86_64/libbig.so", elf64(ALIGN_64K)));

        assertEquals(List.of(), NativeLibAlignment.checkApk(apk, PAGE));
    }

    @Test
    public void everyLoadSegmentIsChecked() throws IOException {
        // A first segment that looks fine must not hide a 4 KB one behind it.
        Path apk = apk(deflated("lib/arm64-v8a/libmixed.so", elf64(ALIGN_16K, ALIGN_4K)));

        List<String> problems = NativeLibAlignment.checkApk(apk, PAGE);

        assertEquals(problems.toString(), 1, problems.size());
        assertTrue(problems.get(0), problems.get(0).contains("0x1000"));
    }

    @Test
    public void nonLoadSegmentsDoNotCount() throws IOException {
        // PT_DYNAMIC / PT_GNU_STACK etc. carry small alignments by design.
        byte[] elf = elf64WithSegments(new long[][] {{PT_LOAD, ALIGN_16K}, {PT_DYNAMIC, 8}, {PT_GNU_STACK, 16}});
        Path apk = apk(deflated("lib/arm64-v8a/libok.so", elf));

        assertEquals(List.of(), NativeLibAlignment.checkApk(apk, PAGE));
    }

    @Test
    public void a32BitLibraryIsNotHeldTo16KbPages() throws IOException {
        // 16 KB pages exist only on 64-bit devices; 32-bit ABIs keep 4 KB alignment.
        Path apk = apk(deflated("lib/armeabi-v7a/libtermux.so", elf32(ALIGN_4K)));

        assertEquals(List.of(), NativeLibAlignment.checkApk(apk, PAGE));
    }

    @Test
    public void aBigEndianHeaderIsReadCorrectly() throws IOException {
        Path apk = apk(deflated("lib/arm64-v8a/libbe.so", elf64(ByteOrder.BIG_ENDIAN, ALIGN_4K)));

        List<String> problems = NativeLibAlignment.checkApk(apk, PAGE);

        assertEquals(problems.toString(), 1, problems.size());
        assertTrue(problems.get(0), problems.get(0).contains("0x1000"));
    }

    // ---- fail closed on anything the gate cannot vouch for -------------------------------

    @Test
    public void aLibraryThatIsNotElfIsRejected() throws IOException {
        Path apk = apk(deflated("lib/arm64-v8a/libfake.so", "#!/bin/sh\necho hi\n".getBytes()));

        List<String> problems = NativeLibAlignment.checkApk(apk, PAGE);

        assertEquals(problems.toString(), 1, problems.size());
        assertTrue(problems.get(0), problems.get(0).contains("lib/arm64-v8a/libfake.so"));
    }

    @Test
    public void aTruncatedElfIsRejected() throws IOException {
        byte[] whole = elf64(ALIGN_16K);
        Path apk = apk(deflated("lib/arm64-v8a/libcut.so", Arrays.copyOf(whole, 70)));

        assertEquals(1, NativeLibAlignment.checkApk(apk, PAGE).size());
    }

    @Test
    public void programHeadersPointingPastTheFileAreRejected() throws IOException {
        byte[] elf = elf64(ALIGN_16K);
        ByteBuffer.wrap(elf).order(ByteOrder.LITTLE_ENDIAN).putLong(0x20, 1L << 40); // e_phoff
        Path apk = apk(deflated("lib/arm64-v8a/libwild.so", elf));

        assertEquals(1, NativeLibAlignment.checkApk(apk, PAGE).size());
    }

    @Test
    public void a64BitLibraryWithNoLoadSegmentIsRejected() throws IOException {
        byte[] elf = elf64WithSegments(new long[][] {{PT_DYNAMIC, 8}});
        Path apk = apk(deflated("lib/arm64-v8a/libempty.so", elf));

        assertEquals(1, NativeLibAlignment.checkApk(apk, PAGE).size());
    }

    // ---- zip alignment of uncompressed libraries -----------------------------------------

    @Test
    public void anUncompressedLibraryAtA16KbOffsetPasses() throws IOException {
        // AGP stores libraries uncompressed (useLegacyPackaging = false) so they are mmapped
        // straight from the APK; their data must then start on a page boundary.
        Path apk = apk(storedAligned("lib/arm64-v8a/libLiteRt.so", elf64(ALIGN_16K), PAGE));

        assertEquals(List.of(), NativeLibAlignment.checkApk(apk, PAGE));
    }

    @Test
    public void anUncompressedLibraryOffThePageBoundaryIsRejected() throws IOException {
        Path apk = apk(
            deflated("classes.dex", new byte[1000]),
            stored("lib/arm64-v8a/libLiteRt.so", elf64(ALIGN_16K)));

        List<String> problems = NativeLibAlignment.checkApk(apk, PAGE);

        assertEquals(problems.toString(), 1, problems.size());
        assertTrue(problems.get(0), problems.get(0).contains("lib/arm64-v8a/libLiteRt.so"));
        assertTrue(problems.get(0), problems.get(0).contains("offset"));
    }

    @Test
    public void aCompressedLibraryNeedsNoZipAlignment() throws IOException {
        // Deflated libraries are extracted at install time (legacy packaging), so only the
        // ELF alignment matters.
        Path apk = apk(
            deflated("classes.dex", new byte[1000]),
            deflated("lib/arm64-v8a/libLiteRt.so", elf64(ALIGN_16K)));

        assertEquals(List.of(), NativeLibAlignment.checkApk(apk, PAGE));
    }

    // ---- scope ---------------------------------------------------------------------------

    @Test
    public void filesOutsideLibAreIgnored() throws IOException {
        Path apk = apk(
            deflated("assets/model.so", "not a library".getBytes()),
            deflated("lib/arm64-v8a/README.txt", "notes".getBytes()));

        assertEquals(List.of(), NativeLibAlignment.checkApk(apk, PAGE));
    }

    @Test
    public void everyOffendingLibraryIsReported() throws IOException {
        Path apk = apk(
            deflated("lib/arm64-v8a/libone.so", elf64(ALIGN_4K)),
            deflated("lib/arm64-v8a/libgood.so", elf64(ALIGN_16K)),
            deflated("lib/x86_64/libtwo.so", elf64(ALIGN_4K)));

        assertEquals(2, NativeLibAlignment.checkApk(apk, PAGE).size());
    }

    @Test
    public void anApkWithoutNativeLibrariesPasses() throws IOException {
        Path apk = apk(deflated("classes.dex", new byte[64]));

        assertEquals(List.of(), NativeLibAlignment.checkApk(apk, PAGE));
    }

    @Test
    public void aFileThatIsNotAZipIsRejected() throws IOException {
        Path notApk = tmp.newFile("broken.apk").toPath();
        Files.write(notApk, new byte[] {1, 2, 3, 4});

        assertEquals(1, NativeLibAlignment.checkApk(notApk, PAGE).size());
    }

    // ---- merged libraries, checked before any APK is written -----------------------------

    @Test
    public void aMerged4KbLibraryIsRejectedBeforePackaging() throws IOException {
        Path libs = libDir(Map.of(
            "lib/arm64-v8a/libtermux.so", elf64(ALIGN_4K, ALIGN_4K),
            "lib/arm64-v8a/libLiteRt.so", elf64(ALIGN_16K)));

        List<String> problems = NativeLibAlignment.checkLibraryDirectory(libs, Set.of(), PAGE);

        assertEquals(problems.toString(), 1, problems.size());
        assertTrue(problems.get(0), problems.get(0).startsWith("lib/arm64-v8a/libtermux.so: "));
        assertTrue(problems.get(0), problems.get(0).contains("0x1000"));
    }

    @Test
    public void mergedLibrariesThatAreAllAlignedPass() throws IOException {
        Path libs = libDir(Map.of(
            "lib/arm64-v8a/libtermux.so", elf64(ALIGN_16K, ALIGN_16K),
            "lib/arm64-v8a/liblitertlm_jni.so", elf64(ALIGN_64K),
            "lib/armeabi-v7a/libtermux.so", elf32(ALIGN_4K)));

        assertEquals(List.of(), NativeLibAlignment.checkLibraryDirectory(libs, Set.of(), PAGE));
    }

    @Test
    public void anAbiTheVariantFiltersOutIsNotChecked() throws IOException {
        // AGP merges every ABI its dependencies ship and applies ndk.abiFilters only when it
        // packages, so an x86_64 copy that never reaches the APK must not fail the build.
        Path libs = libDir(Map.of(
            "lib/arm64-v8a/libtermux.so", elf64(ALIGN_16K),
            "lib/x86_64/libtermux.so", elf64(ALIGN_4K)));

        assertEquals(List.of(),
            NativeLibAlignment.checkLibraryDirectory(libs, Set.of("arm64-v8a"), PAGE));
        assertEquals("with no filter every ABI is packaged, so every ABI is checked", 1,
            NativeLibAlignment.checkLibraryDirectory(libs, Set.of(), PAGE).size());
    }

    @Test
    public void aFilteredAbiStillHasItsLibrariesChecked() throws IOException {
        Path libs = libDir(Map.of("lib/arm64-v8a/libbad.so", elf64(ALIGN_4K)));

        assertEquals(1, NativeLibAlignment.checkLibraryDirectory(libs, Set.of("arm64-v8a"), PAGE).size());
    }

    @Test
    public void aMergedLibraryThatIsNotElfIsRejected() throws IOException {
        Path libs = libDir(Map.of("lib/arm64-v8a/libfake.so", "not elf".getBytes()));

        List<String> problems = NativeLibAlignment.checkLibraryDirectory(libs, Set.of(), PAGE);

        assertEquals(problems.toString(), 1, problems.size());
        assertTrue(problems.get(0), problems.get(0).startsWith("lib/arm64-v8a/libfake.so: "));
    }

    @Test
    public void aLibraryOutsideTheAbiLayoutIsRejected() throws IOException {
        // Fail closed: a .so the check cannot place under lib/<abi>/ is not waved through.
        Path libs = libDir(Map.of("libstray.so", elf64(ALIGN_4K)));

        List<String> problems = NativeLibAlignment.checkLibraryDirectory(libs, Set.of("arm64-v8a"), PAGE);

        assertEquals(problems.toString(), 1, problems.size());
        assertTrue(problems.get(0), problems.get(0).startsWith("libstray.so: "));
    }

    @Test
    public void otherMergedFilesAreIgnored() throws IOException {
        Path libs = libDir(Map.of(
            "lib/arm64-v8a/README.txt", "notes".getBytes(),
            "lib/arm64-v8a/gdbserver", "#!/bin/sh".getBytes()));

        assertEquals(List.of(), NativeLibAlignment.checkLibraryDirectory(libs, Set.of(), PAGE));
    }

    @Test
    public void anEmptyMergeOutputPasses() throws IOException {
        assertEquals(List.of(),
            NativeLibAlignment.checkLibraryDirectory(tmp.newFolder("none").toPath(), Set.of(), PAGE));
    }

    // ---- the Gradle task that runs before package<Variant> -------------------------------

    @Test
    public void thePrePackageTaskFailsTheBuildOnA4KbLibrary() throws IOException {
        Path libs = libDir(Map.of(
            "lib/arm64-v8a/libtermux.so", elf64(ALIGN_4K),
            "lib/x86_64/libtermux.so", elf64(ALIGN_4K)));
        CheckMergedNativeLibAlignment task = mergedLibsTask(libs, Set.of("arm64-v8a"));

        GradleException failure = assertThrows(GradleException.class, task::check);

        assertTrue(failure.getMessage(), failure.getMessage().contains("lib/arm64-v8a/libtermux.so"));
        assertFalse("a filtered-out ABI is not reported: " + failure.getMessage(),
            failure.getMessage().contains("lib/x86_64/"));
        String report = Files.readString(task.getReport().get().getAsFile().toPath());
        assertTrue(report, report.contains("lib/arm64-v8a/libtermux.so"));
    }

    @Test
    public void thePrePackageTaskPassesAlignedLibraries() throws IOException {
        Path libs = libDir(Map.of("lib/arm64-v8a/libtermux.so", elf64(ALIGN_16K)));
        CheckMergedNativeLibAlignment task = mergedLibsTask(libs, Set.of("arm64-v8a"));

        task.check();

        assertEquals(List.of("OK: 1 native library checked for arm64-v8a"),
            Files.readAllLines(task.getReport().get().getAsFile().toPath()));
    }

    private CheckMergedNativeLibAlignment mergedLibsTask(Path libs, Set<String> abis) throws IOException {
        Project project = ProjectBuilder.builder().withProjectDir(tmp.newFolder()).build();
        CheckMergedNativeLibAlignment task = project.getTasks()
            .register("checkMergedNativeLibAlignment", CheckMergedNativeLibAlignment.class).get();
        task.getNativeLibsDirectory().set(libs.toFile());
        task.getAbis().set(abis);
        task.getReport().set(project.getLayout().getBuildDirectory().file("merged-alignment.txt"));
        return task;
    }

    /** A merged-native-libs directory (AGP's out/ layout) holding {@code files}. */
    private Path libDir(Map<String, byte[]> files) throws IOException {
        Path root = tmp.newFolder().toPath();
        for (Map.Entry<String, byte[]> f : files.entrySet()) {
            Path target = root.resolve(f.getKey());
            Files.createDirectories(target.getParent());
            Files.write(target, f.getValue());
        }
        return root;
    }

    // ---- fixtures ------------------------------------------------------------------------

    private static final int PT_LOAD = 1;
    private static final int PT_DYNAMIC = 2;
    private static final int PT_GNU_STACK = 0x6474e551;

    private static byte[] elf64(long... loadAligns) {
        return elf64(ByteOrder.LITTLE_ENDIAN, loadAligns);
    }

    private static byte[] elf64(ByteOrder order, long... loadAligns) {
        long[][] segments = new long[loadAligns.length][];
        for (int i = 0; i < loadAligns.length; i++) {
            segments[i] = new long[] {PT_LOAD, loadAligns[i]};
        }
        return elf64WithSegments(order, segments);
    }

    private static byte[] elf64WithSegments(long[][] segments) {
        return elf64WithSegments(ByteOrder.LITTLE_ENDIAN, segments);
    }

    /** A minimal ELF64 ET_DYN header plus one 56-byte program header per {type, align}. */
    private static byte[] elf64WithSegments(ByteOrder order, long[][] segments) {
        int phoff = 64;
        ByteBuffer b = ByteBuffer.allocate(phoff + 56 * segments.length).order(order);
        b.put(new byte[] {0x7f, 'E', 'L', 'F', 2, (byte) (order == ByteOrder.LITTLE_ENDIAN ? 1 : 2), 1});
        b.position(16);
        b.putShort((short) 3);       // e_type ET_DYN
        b.putShort((short) 183);     // e_machine EM_AARCH64
        b.putInt(1);                 // e_version
        b.putLong(0);                // e_entry
        b.putLong(phoff);            // e_phoff
        b.putLong(0);                // e_shoff
        b.putInt(0);                 // e_flags
        b.putShort((short) 64);      // e_ehsize
        b.putShort((short) 56);      // e_phentsize
        b.putShort((short) segments.length);
        b.position(phoff);
        for (long[] s : segments) {
            b.putInt((int) s[0]);    // p_type
            b.putInt(5);             // p_flags
            b.putLong(0);            // p_offset
            b.putLong(0);            // p_vaddr
            b.putLong(0);            // p_paddr
            b.putLong(0);            // p_filesz
            b.putLong(0);            // p_memsz
            b.putLong(s[1]);         // p_align
        }
        return b.array();
    }

    /** A minimal little-endian ELF32 header with one PT_LOAD per alignment. */
    private static byte[] elf32(int... loadAligns) {
        int phoff = 52;
        ByteBuffer b = ByteBuffer.allocate(phoff + 32 * loadAligns.length).order(ByteOrder.LITTLE_ENDIAN);
        b.put(new byte[] {0x7f, 'E', 'L', 'F', 1, 1, 1});
        b.position(16);
        b.putShort((short) 3);       // e_type
        b.putShort((short) 40);      // e_machine EM_ARM
        b.putInt(1);                 // e_version
        b.putInt(0);                 // e_entry
        b.putInt(phoff);             // e_phoff
        b.putInt(0);                 // e_shoff
        b.putInt(0);                 // e_flags
        b.putShort((short) 52);      // e_ehsize
        b.putShort((short) 32);      // e_phentsize
        b.putShort((short) loadAligns.length);
        b.position(phoff);
        for (int align : loadAligns) {
            b.putInt(PT_LOAD);
            b.putInt(0);             // p_offset
            b.putInt(0);             // p_vaddr
            b.putInt(0);             // p_paddr
            b.putInt(0);             // p_filesz
            b.putInt(0);             // p_memsz
            b.putInt(5);             // p_flags
            b.putInt(align);         // p_align
        }
        return b.array();
    }

    private record Entry(String name, byte[] data, boolean stored, int alignTo) {}

    private static Entry deflated(String name, byte[] data) {
        return new Entry(name, data, false, 0);
    }

    private static Entry stored(String name, byte[] data) {
        return new Entry(name, data, true, 0);
    }

    /** Stored, padded with a zipalign-style 0xD935 extra field so the data lands on alignTo. */
    private static Entry storedAligned(String name, byte[] data, int alignTo) {
        return new Entry(name, data, true, alignTo);
    }

    private Path apk(Entry... entries) throws IOException {
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        try (ZipOutputStream zip = new ZipOutputStream(bytes)) {
            long offset = 0;
            for (Entry e : entries) {
                ZipEntry z = new ZipEntry(e.name());
                byte[] extra = new byte[0];
                if (e.stored()) {
                    z.setMethod(ZipEntry.STORED);
                    z.setSize(e.data().length);
                    z.setCompressedSize(e.data().length);
                    CRC32 crc = new CRC32();
                    crc.update(e.data());
                    z.setCrc(crc.getValue());
                    if (e.alignTo() > 0) {
                        long dataStart = offset + 30 + e.name().length();
                        int pad = (int) ((e.alignTo() - (dataStart + 6) % e.alignTo()) % e.alignTo());
                        extra = ByteBuffer.allocate(6 + pad).order(ByteOrder.LITTLE_ENDIAN)
                            .putShort((short) 0xD935).putShort((short) (2 + pad)).putShort((short) e.alignTo())
                            .array();
                        z.setExtra(extra);
                    }
                }
                zip.putNextEntry(z);
                zip.write(e.data());
                zip.closeEntry();
                zip.flush();
                offset = bytes.size();
            }
        }
        Path apk = tmp.newFile().toPath();
        Files.write(apk, bytes.toByteArray());
        return apk;
    }
}
