pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    id: pane
    required property real unit
    required property real gutter
    required property bool dialogOpen
    readonly property var queue: library.queueModel
    readonly property bool narrow: width < 850 * unit
    readonly property real phaseWidth: (narrow ? 155 : width < 1100 * unit ? 175 : 222) * unit
    readonly property real speedWidth: (width < 1100 * unit ? 140 : 174) * unit
    readonly property real cpuWidth: (width < 1100 * unit ? 116 : 142) * unit
    readonly property real columnGap: (narrow ? 14 : 22) * unit
    readonly property real rowHeight: (height < 600 * unit ? 67 : 79) * unit
    readonly property var stageNames: ({queued: "Queued", preparing: "Preparing", compressing: "Compressing",
        verifying: "Verifying", publishing: "Publishing", done: "Verified", failed: "Failed",
        cancelled: "Cancelled", paused: "Paused", skipped: "Skipped"})
    property bool following: true
    property int selected: 0
    spacing: 0

    function selectRow(index) {
        if (!discs.count) return
        selected = Math.max(0, Math.min(discs.count - 1, index))
        discs.positionViewAtIndex(selected, ListView.Contain)
    }
    function followActive() {
        if (following && visible && !dialogOpen && queue.activeIndex >= 0) selectRow(queue.activeIndex)
    }
    function navigate(action) {
        if (action === "inspect" && queue.activeIndex >= 0) {
            following = true
            followActive()
        } else if (action === "confirm" && discs.count) library.inspectQueueItem(selected)
        else if (["up", "down", "left", "right"].includes(action) && discs.count) {
            following = false
            let page = Math.max(1, Math.floor(discs.height / rowHeight))
            selectRow(selected + ({up: -1, down: 1, left: -page, right: page})[action])
        } else return false
        return true
    }
    onDialogOpenChanged: if (!dialogOpen) followActive()
    Connections {
        target: pane.queue
        function onActiveChanged() { pane.followActive() }
        function onModelReset() { pane.following = true; pane.selected = 0; discs.positionViewAtBeginning() }
    }

    RowLayout {
        Layout.fillWidth: true; Layout.leftMargin: pane.gutter; Layout.rightMargin: pane.gutter
        Layout.topMargin: (pane.height < 600 * pane.unit ? 12 : 24) * pane.unit
        spacing: pane.columnGap
        Text {
            Layout.fillWidth: true; text: library.queueTitle; color: "#f3f4eb"
            font.pixelSize: (pane.narrow ? 33 : 42) * pane.unit; font.weight: Font.DemiBold; font.letterSpacing: -1
            elide: Text.ElideRight
        }
        Text {
            text: library.queueCancelling ? "Cancelling safely…" : library.queueResult
            visible: text.length > 0; color: "#e1ebca"; font.pixelSize: (pane.narrow ? 18 : 22) * pane.unit
        }
    }
    RowLayout {
        Layout.fillWidth: true; Layout.leftMargin: pane.gutter; Layout.rightMargin: pane.gutter
        Layout.topMargin: 5 * pane.unit; Layout.bottomMargin: (pane.height < 600 * pane.unit ? 12 : 22) * pane.unit
        Text {
            Layout.fillWidth: true; font.pixelSize: (pane.narrow ? 19 : 22) * pane.unit; color: "#d3dfcb"
            text: pane.queue.total + (pane.queue.total === 1 ? " disc" : " discs") + "   ·   " + pane.queue.activeCount + " active   ·   " + pane.queue.verified + " verified"
        }
        Text {
            visible: !pane.narrow && pane.queue.total > 0; font.pixelSize: 19 * pane.unit; color: "#b7c4b8"
            text: (pane.following && pane.queue.activeIndex >= 0 ? "Following disc " : "Viewing disc ") + (pane.selected + 1) + " of " + pane.queue.total
        }
    }
    component Heading: Text {
        color: "#bac7b8"; font.pixelSize: 18 * pane.unit; font.weight: Font.DemiBold; elide: Text.ElideRight
    }
    RowLayout {
        Layout.fillWidth: true; Layout.leftMargin: pane.gutter + 16 * pane.unit; Layout.rightMargin: pane.gutter + 26 * pane.unit
        Layout.bottomMargin: 9 * pane.unit; spacing: pane.columnGap
        Heading { Layout.fillWidth: true; text: "Source disc" }
        Heading { Layout.preferredWidth: pane.phaseWidth; text: "Phase" }
        Heading { visible: !pane.narrow; Layout.preferredWidth: pane.speedWidth; horizontalAlignment: Text.AlignRight; text: "Est. input speed" }
        Heading { visible: !pane.narrow; Layout.preferredWidth: pane.cpuWidth; horizontalAlignment: Text.AlignRight; text: "Device CPU" }
    }
    Rectangle {
        Layout.fillWidth: true; Layout.fillHeight: true; Layout.leftMargin: pane.gutter; Layout.rightMargin: pane.gutter
        color: "#242f28"; radius: 6 * pane.unit
        ListView {
            id: discs; objectName: "queueList"; anchors.fill: parent; clip: true
            model: pane.queue; currentIndex: pane.selected; keyNavigationEnabled: false
            boundsBehavior: Flickable.StopAtBounds; reuseItems: true
            onMovementStarted: pane.following = false
            ScrollBar.vertical: ScrollBar { onPressedChanged: if (pressed) pane.following = false }
            delegate: Rectangle {
                id: disc
                required property int index
                required property string discName
                required property string systemName
                required property string phase
                required property real phasePercent
                required property real inputSpeed
                required property real deviceCpu
                required property string itemDetail
                readonly property bool chosen: index === pane.selected && !pane.dialogOpen
                readonly property string stageName: pane.stageNames[phase] || phase
                readonly property string pct: phasePercent < 0 ? "" : Number(phasePercent.toFixed(1)).toString() + "%"
                readonly property string speed: inputSpeed < 0 ? "" : inputSpeed.toFixed(1) + " MiB/s"
                readonly property string cpu: deviceCpu < 0 ? "" : Number(deviceCpu.toFixed(1)).toString() + "%"
                readonly property string compactMetrics: [speed ? "Est. " + speed : "", cpu ? cpu + " device CPU" : ""].filter(s => s.length).join("   ·   ")
                readonly property color stageColor: phase === "failed" ? "#ffbcaf" : ["paused", "cancelled"].includes(phase) ? "#f3ce94" : ["done", "compressing", "verifying"].includes(phase) ? "#d9eea6" : "#c3d0c2"
                width: discs.width; height: pane.rowHeight
                color: chosen ? "#34432f" : over.hovered ? "#2e3a31" : "transparent"
                border.color: chosen ? "#b9d889" : "transparent"; border.width: chosen ? 2 : 0; radius: 5 * pane.unit
                Accessible.role: Accessible.ListItem; Accessible.selected: chosen; Accessible.focused: chosen
                Accessible.name: [discName, systemName, stageName, pct, speed ? "Estimated input speed " + speed : "", cpu ? "Device CPU " + cpu : "", itemDetail].filter(s => s.length).join(". ")
                function inspect() { pane.following = false; pane.selectRow(index); library.inspectQueueItem(index) }
                Accessible.onPressAction: inspect()
                HoverHandler { id: over }
                TapHandler { onTapped: disc.inspect() }
                RowLayout {
                    anchors.fill: parent; anchors.leftMargin: 16 * pane.unit; anchors.rightMargin: 26 * pane.unit; spacing: pane.columnGap
                    ColumnLayout {
                        Layout.fillWidth: true; spacing: 1 * pane.unit
                        Text {
                            Layout.fillWidth: true; text: disc.discName; textFormat: Text.PlainText; elide: Text.ElideMiddle
                            color: "#f2f4e9"; font.pixelSize: (pane.narrow ? 20 : 23) * pane.unit; font.weight: Font.DemiBold
                        }
                        Text {
                            Layout.fillWidth: true; textFormat: Text.PlainText; elide: Text.ElideRight
                            text: disc.systemName + (pane.narrow && disc.compactMetrics ? "   ·   " + disc.compactMetrics : disc.itemDetail ? "   ·   " + disc.itemDetail : "")
                            color: "#bfcbbb"; font.pixelSize: (pane.narrow ? 16 : 18) * pane.unit
                        }
                    }
                    ColumnLayout {
                        Layout.preferredWidth: pane.phaseWidth; Layout.minimumWidth: pane.phaseWidth; Layout.maximumWidth: pane.phaseWidth
                        spacing: 6 * pane.unit
                        RowLayout {
                            Layout.fillWidth: true; spacing: 3 * pane.unit
                            Text { Layout.fillWidth: true; text: disc.stageName; color: disc.stageColor; font.pixelSize: (pane.narrow ? 17 : 20) * pane.unit; elide: Text.ElideRight }
                            Text { visible: !!disc.pct; text: disc.pct; font.pixelSize: (pane.narrow ? 16 : 19) * pane.unit; color: "#edf3e4" }
                        }
                        Rectangle {
                            visible: disc.phasePercent >= 0; Layout.fillWidth: true; Layout.preferredHeight: 6 * pane.unit
                            color: "#57634e"; radius: height / 2
                            Rectangle { height: parent.height; radius: parent.radius; width: parent.width * Math.max(0, Math.min(100, disc.phasePercent)) / 100; color: "#d1eb96" }
                        }
                    }
                    Text { visible: !pane.narrow; Layout.preferredWidth: pane.speedWidth; horizontalAlignment: Text.AlignRight; text: disc.speed; color: "#edf2e7"; font.pixelSize: 21 * pane.unit }
                    Text { visible: !pane.narrow; Layout.preferredWidth: pane.cpuWidth; horizontalAlignment: Text.AlignRight; text: disc.cpu; color: "#edf2e7"; font.pixelSize: 21 * pane.unit }
                }
                Rectangle { visible: !disc.chosen; anchors.bottom: parent.bottom; anchors.left: parent.left; anchors.right: parent.right; anchors.leftMargin: 16 * pane.unit; anchors.rightMargin: 16 * pane.unit; height: 1; color: "#3b483e" }
            }
        }
        Text {
            anchors.centerIn: parent; width: parent.width - 30; horizontalAlignment: Text.AlignHCenter; wrapMode: Text.Wrap
            visible: !pane.queue.total; text: library.queueResult ? "No source discs entered this queue." : "Looking for source discs…"
            font.pixelSize: 23 * pane.unit; color: "#c4d0c2"
        }
    }
    Text {
        Layout.fillWidth: true; Layout.leftMargin: pane.gutter; Layout.rightMargin: pane.gutter
        Layout.topMargin: 9 * pane.unit; Layout.bottomMargin: 10 * pane.unit
        text: "Speed estimates logical input processed. CPU is this disc’s share of total device capacity."
        color: "#bdcbbb"; font.pixelSize: (pane.narrow ? 16 : 18) * pane.unit; wrapMode: Text.Wrap
    }
    Rectangle {
        Layout.fillWidth: true; Layout.preferredHeight: 51 * pane.unit; color: "#111a16"
        RowLayout {
            anchors.fill: parent; anchors.leftMargin: pane.gutter; anchors.rightMargin: pane.gutter; spacing: 13 * pane.unit
            Command { compact: true; unit: pane.unit; label: "Details"; glyph: router.glyphs[0]; enabled: pane.queue.total > 0; onActivated: pane.navigate("confirm") }
            Command { compact: true; unit: pane.unit; label: library.operation === "Compressing discs" ? "Cancel" : "Back"; glyph: router.glyphs[1]; enabled: !library.busy || (library.operation === "Compressing discs" && !library.queueCancelling); onActivated: library.back() }
            Command { compact: true; unit: pane.unit; label: "Follow active"; glyph: router.glyphs[2]; visible: pane.queue.activeIndex >= 0; onActivated: pane.navigate("inspect") }
            Item { Layout.fillWidth: true }
            Text { visible: !pane.narrow; text: "Up / down to browse"; color: "#bdcbbb"; font.pixelSize: 18 * pane.unit }
        }
    }
}
