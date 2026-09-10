import QtQuick
import QtTest
import ".." as Pulse

TestCase {
    name: "DiskPulseWidgets"
    when: windowShown
    // The chip only paints while it is actually on screen, so the case itself
    // has to be shown or nothing under test ever repaints.
    visible: true
    width: 740; height: 240

    Pulse.DiskChip { id: chip; width: 28; height: 25; compact: true; animate: false; free: 32 }
    Pulse.HistoryGraph { id: graph; width: 700; height: 139 }

    function test_phase_advances_only_while_animated_and_visible() {
        failOnWarning(/.*/)
        chip.animate = false
        chip.phase = 0
        wait(250)
        compare(chip.phase, 0, 'a still chip must not tick')
        chip.animate = true
        tryVerify(function() { return chip.phase > 0 }, 2000)
        chip.visible = false
        var frozen = chip.phase
        wait(300)
        compare(chip.phase, frozen, 'an off-screen chip must not tick')
        chip.visible = true
        tryVerify(function() { return chip.phase > frozen }, 2000)
        chip.animate = false
    }

    function test_phase_wraps_without_leaving_the_unit_range() {
        failOnWarning(/.*/)
        chip.animate = false
        chip.phase = 0.99
        chip.animate = true
        tryVerify(function() { return chip.phase < 0.5 }, 2000)
        verify(chip.phase >= 0)
        chip.animate = false
    }

    function test_used_follows_free() {
        failOnWarning(/.*/)
        chip.free = 25
        fuzzyCompare(chip.used, 0.75, 0.001)
        chip.free = 140
        compare(chip.used, 0, 'free is clamped to the scale')
        chip.free = 32
    }

    function test_empty_and_replaced_history() {
        failOnWarning(/.*/)
        graph.historyData = {points: [[100, 12e6, 40e6, 3e6, 67.5, 12, 1, 'boot']], seconds: 3600, now: 100, bucket: 15, peakRead: 40e6, peakWrite: 3e6}
        graph.hoverIndex = 0
        compare(graph.points.length, 1)
        compare(graph.ceiling, 5e7)
        graph.historyData = {points: [], seconds: 3600, now: 101, bucket: 15}
        compare(graph.hoverIndex, -1)
        compare(graph.points.length, 0)
        wait(30)
    }
}
