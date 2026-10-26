package com.aiblackbox.buildlogic;

import java.io.File;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.stream.Stream;
import org.gradle.api.DefaultTask;
import org.gradle.api.GradleException;
import org.gradle.api.file.DirectoryProperty;
import org.gradle.api.file.RegularFileProperty;
import org.gradle.api.provider.Property;
import org.gradle.api.tasks.Input;
import org.gradle.api.tasks.InputDirectory;
import org.gradle.api.tasks.OutputFile;
import org.gradle.api.tasks.PathSensitive;
import org.gradle.api.tasks.PathSensitivity;
import org.gradle.api.tasks.TaskAction;

/**
 * Fails the build when an APK in {@link #getApkDirectory()} carries a native library that a
 * 16 KB-page device cannot load (see {@link NativeLibAlignment}). The app wires one per
 * variant onto AGP's final APK artifact and makes {@code assemble<Variant>} and
 * {@code install<Variant>} depend on it, so a 4 KB-aligned library can never reach an
 * installable artifact unnoticed.
 */
public abstract class CheckNativeLibAlignment extends DefaultTask {

    /** The variant's APK output directory (AGP's SingleArtifact.APK). */
    @InputDirectory
    @PathSensitive(PathSensitivity.RELATIVE)
    public abstract DirectoryProperty getApkDirectory();

    /** Required alignment in bytes; 16 KB unless a caller has a reason to differ. */
    @Input
    public abstract Property<Integer> getPageSize();

    /** One line per checked APK and per problem, kept for CI logs and up-to-date checks. */
    @OutputFile
    public abstract RegularFileProperty getReport();

    public CheckNativeLibAlignment() {
        getPageSize().convention(NativeLibAlignment.PAGE_SIZE_16K);
        setGroup("verification");
        setDescription("Fails if a native library in the APK is not 16 KB page-aligned.");
    }

    @TaskAction
    public void check() throws IOException {
        int pageSize = getPageSize().get();
        List<Path> apks;
        try (Stream<Path> files = Files.walk(getApkDirectory().get().getAsFile().toPath())) {
            apks = files.filter(p -> p.getFileName().toString().endsWith(".apk")).sorted().toList();
        }
        if (apks.isEmpty()) {
            throw new GradleException("No APK found in " + getApkDirectory().get().getAsFile());
        }

        List<String> report = new ArrayList<>();
        List<String> failures = new ArrayList<>();
        for (Path apk : apks) {
            List<String> problems = NativeLibAlignment.checkApk(apk, pageSize);
            report.add(apk.getFileName() + ": " + (problems.isEmpty() ? "OK" : problems.size() + " problem(s)"));
            for (String problem : problems) {
                report.add("  " + problem);
                failures.add(apk.getFileName() + ": " + problem);
            }
        }
        File reportFile = getReport().get().getAsFile();
        Files.createDirectories(reportFile.getParentFile().toPath());
        Files.write(reportFile.toPath(), report, StandardCharsets.UTF_8);

        if (!failures.isEmpty()) {
            throw new GradleException("Native libraries are not " + (pageSize / 1024)
                + " KB page-aligned, so the APK cannot load them on 16 KB-page devices (Android 15+):\n  "
                + String.join("\n  ", failures)
                + "\nReport: " + reportFile);
        }
    }
}
