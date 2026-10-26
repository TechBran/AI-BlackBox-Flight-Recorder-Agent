package com.aiblackbox.buildlogic;

import java.io.File;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import java.util.TreeSet;
import java.util.stream.Stream;
import org.gradle.api.DefaultTask;
import org.gradle.api.GradleException;
import org.gradle.api.file.DirectoryProperty;
import org.gradle.api.file.RegularFileProperty;
import org.gradle.api.provider.Property;
import org.gradle.api.provider.SetProperty;
import org.gradle.api.tasks.Input;
import org.gradle.api.tasks.InputDirectory;
import org.gradle.api.tasks.OutputFile;
import org.gradle.api.tasks.PathSensitive;
import org.gradle.api.tasks.PathSensitivity;
import org.gradle.api.tasks.TaskAction;

/**
 * Fails the build when a native library the variant is about to package cannot be loaded on a
 * 16 KB-page device ({@link NativeLibAlignment#checkLibraryDirectory}). The app wires one per
 * variant onto AGP's MERGED_NATIVE_LIBS artifact and makes {@code package<Variant>} depend on
 * it, so a 4 KB-aligned library fails the build before any APK is written: nothing is left on
 * the output path to be installed. {@link CheckNativeLibAlignment} then checks the APK itself.
 */
public abstract class CheckMergedNativeLibAlignment extends DefaultTask {

    /** The variant's merged native libraries ({@code lib/<abi>/*.so}). */
    @InputDirectory
    @PathSensitive(PathSensitivity.RELATIVE)
    public abstract DirectoryProperty getNativeLibsDirectory();

    /** The ABIs the variant packages (ndk.abiFilters); empty means every ABI. */
    @Input
    public abstract SetProperty<String> getAbis();

    /** Required alignment in bytes; 16 KB unless a caller has a reason to differ. */
    @Input
    public abstract Property<Integer> getPageSize();

    /** The result, kept for CI logs and up-to-date checks. */
    @OutputFile
    public abstract RegularFileProperty getReport();

    public CheckMergedNativeLibAlignment() {
        getAbis().convention(Set.of());
        getPageSize().convention(NativeLibAlignment.PAGE_SIZE_16K);
        setGroup("verification");
        setDescription("Fails before packaging if a native library is not 16 KB page-aligned.");
    }

    @TaskAction
    public void check() throws IOException {
        int pageSize = getPageSize().get();
        Set<String> abis = new TreeSet<>(getAbis().get());
        Path root = getNativeLibsDirectory().get().getAsFile().toPath();
        List<String> problems = NativeLibAlignment.checkLibraryDirectory(root, abis, pageSize);

        List<String> report = new ArrayList<>();
        if (problems.isEmpty()) {
            long checked = countChecked(root, abis);
            report.add("OK: " + checked + " native " + (checked == 1 ? "library" : "libraries")
                + " checked for " + (abis.isEmpty() ? "every ABI" : String.join(", ", abis)));
        } else {
            report.addAll(problems);
        }
        File reportFile = getReport().get().getAsFile();
        Files.createDirectories(reportFile.getParentFile().toPath());
        Files.write(reportFile.toPath(), report, StandardCharsets.UTF_8);

        if (!problems.isEmpty()) {
            throw new GradleException("Native libraries are not " + (pageSize / 1024)
                + " KB page-aligned, so the APK could not load them on 16 KB-page devices (Android 15+)."
                + " No APK was packaged:\n  "
                + String.join("\n  ", problems)
                + "\nReport: " + reportFile);
        }
    }

    private static long countChecked(Path root, Set<String> abis) throws IOException {
        try (Stream<Path> walk = Files.walk(root)) {
            return walk.filter(Files::isRegularFile)
                .map(p -> root.relativize(p).toString().replace(File.separatorChar, '/'))
                .filter(NativeLibAlignment::isNativeLibrary)
                .filter(n -> abis.isEmpty() || abis.contains(n.split("/", -1)[1]))
                .count();
        }
    }
}
