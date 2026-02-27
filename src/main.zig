const std = @import("std");

pub export fn framework_version() callconv(.c) i32 {
    return 1;
}

test "framework_version returns 1" {
    try std.testing.expectEqual(@as(i32, 1), framework_version());
}
