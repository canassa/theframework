const std = @import("std");

pub fn build(b: *std.Build) void {
    const target = b.standardTargetOptions(.{});
    const optimize = b.standardOptimizeOption(.{});

    // User-configurable paths (with sensible defaults)
    const python_include = b.option([]const u8, "python-include", "Python include directory (e.g. /usr/include/python3.13)") orelse "/home/canassa/.local/share/uv/python/cpython-3.13.11-linux-x86_64-gnu/include/python3.13";
    const greenlet_include = b.option([]const u8, "greenlet-include", "Greenlet include directory (contains greenlet.h)") orelse ".venv/lib/python3.13/site-packages/greenlet";
    const ext_suffix = b.option([]const u8, "ext-suffix", "Python extension suffix (e.g. .cpython-313-x86_64-linux-gnu.so)") orelse ".cpython-313-x86_64-linux-gnu";

    // -----------------------------------------------------------------------
    // Shared library: libframework.so
    // -----------------------------------------------------------------------
    const lib = b.addLibrary(.{
        .linkage = .dynamic,
        .name = "framework",
        .root_module = b.createModule(.{
            .root_source_file = b.path("src/main.zig"),
            .target = target,
            .optimize = optimize,
            .link_libc = true,
        }),
    });

    b.installArtifact(lib);

    // -----------------------------------------------------------------------
    // Python C extension: _framework_core.<ext_suffix>.so
    // -----------------------------------------------------------------------
    const ext_name = b.fmt("_framework_core{s}", .{ext_suffix});

    const ext_module = b.createModule(.{
        .root_source_file = b.path("src/extension.zig"),
        .target = target,
        .optimize = optimize,
        .link_libc = true,
    });
    ext_module.addIncludePath(b.path("src"));
    ext_module.addIncludePath(.{ .cwd_relative = python_include });
    ext_module.addIncludePath(.{ .cwd_relative = greenlet_include });

    const ext = b.addLibrary(.{
        .linkage = .dynamic,
        .name = ext_name,
        .root_module = ext_module,
    });
    ext.addCSourceFile(.{
        .file = b.path("src/py_helpers.c"),
        .flags = &.{},
    });
    ext.root_module.addIncludePath(b.path("src"));
    ext.root_module.addIncludePath(.{ .cwd_relative = python_include });
    ext.root_module.addIncludePath(.{ .cwd_relative = greenlet_include });

    // Install with the correct name (no "lib" prefix) so Python can import it
    const ext_install = b.addInstallFile(ext.getEmittedBin(), b.fmt("lib/{s}.so", .{ext_name}));
    b.getInstallStep().dependOn(&ext_install.step);

    // -----------------------------------------------------------------------
    // Executable: echo_server
    // -----------------------------------------------------------------------
    const echo_server = b.addExecutable(.{
        .name = "echo_server",
        .root_module = b.createModule(.{
            .root_source_file = b.path("src/echo_server.zig"),
            .target = target,
            .optimize = optimize,
        }),
    });

    b.installArtifact(echo_server);

    // -----------------------------------------------------------------------
    // Executable: echo_server_v2
    // -----------------------------------------------------------------------
    const echo_server_v2 = b.addExecutable(.{
        .name = "echo_server_v2",
        .root_module = b.createModule(.{
            .root_source_file = b.path("src/echo_server_v2.zig"),
            .target = target,
            .optimize = optimize,
        }),
    });

    b.installArtifact(echo_server_v2);

    // -----------------------------------------------------------------------
    // hparse dependency (HTTP parser)
    // -----------------------------------------------------------------------
    const hparse_dep = b.dependency("hparse", .{ .target = target, .optimize = optimize });
    const hparse_mod = hparse_dep.module("hparse");

    // Wire hparse into the extension module so hub.zig can import http.zig
    ext_module.addImport("hparse", hparse_mod);

    // -----------------------------------------------------------------------
    // Tests
    // -----------------------------------------------------------------------

    // Tests for main.zig (existing)
    const lib_tests = b.addTest(.{
        .root_module = b.createModule(.{
            .root_source_file = b.path("src/main.zig"),
            .target = target,
            .optimize = optimize,
        }),
    });
    const run_lib_tests = b.addRunArtifact(lib_tests);

    // Tests for ring.zig
    const ring_tests = b.addTest(.{
        .root_module = b.createModule(.{
            .root_source_file = b.path("src/ring.zig"),
            .target = target,
            .optimize = optimize,
        }),
    });
    const run_ring_tests = b.addRunArtifact(ring_tests);

    // Tests for connection.zig
    const conn_tests = b.addTest(.{
        .root_module = b.createModule(.{
            .root_source_file = b.path("src/connection.zig"),
            .target = target,
            .optimize = optimize,
        }),
    });
    const run_conn_tests = b.addRunArtifact(conn_tests);

    // Tests for http.zig (HTTP request parser using hparse)
    const http_test_mod = b.createModule(.{
        .root_source_file = b.path("src/http.zig"),
        .target = target,
        .optimize = optimize,
    });
    http_test_mod.addImport("hparse", hparse_mod);
    const http_tests = b.addTest(.{ .root_module = http_test_mod });
    const run_http_tests = b.addRunArtifact(http_tests);

    // Tests for http_response.zig (HTTP response writer)
    const http_resp_tests = b.addTest(.{
        .root_module = b.createModule(.{
            .root_source_file = b.path("src/http_response.zig"),
            .target = target,
            .optimize = optimize,
        }),
    });
    const run_http_resp_tests = b.addRunArtifact(http_resp_tests);

    // Tests for op_slot.zig (operation slot table)
    const op_slot_tests = b.addTest(.{
        .root_module = b.createModule(.{
            .root_source_file = b.path("src/op_slot.zig"),
            .target = target,
            .optimize = optimize,
        }),
    });
    const run_op_slot_tests = b.addRunArtifact(op_slot_tests);

    const test_step = b.step("test", "Run tests");
    test_step.dependOn(&run_lib_tests.step);
    test_step.dependOn(&run_ring_tests.step);
    test_step.dependOn(&run_conn_tests.step);
    test_step.dependOn(&run_http_tests.step);
    test_step.dependOn(&run_http_resp_tests.step);
    test_step.dependOn(&run_op_slot_tests.step);
}
