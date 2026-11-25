import QtQuick
import QtQuick.Layouts

ColumnLayout {
    id: panel
    required property var info
    property real unit: 1
    required property string hostKind
    readonly property string phase: info.phase || "idle"
    readonly property bool linked: ["connected", "offered", "sending", "verifying"].includes(phase)
    readonly property color stateColor: linked || phase === "complete" ? "#91e3ac"
        : ["pairing", "approved", "interrupted", "error"].includes(phase) ? "#ffd188" : "#b9c9bd"
    readonly property string phaseText: ({idle: "Not connected", starting: "Opening connection", preparing: "Preparing games",
        waiting: "Waiting for Find sender", pairing: "Found · compare codes", approved: "Confirm on handheld",
        connected: "Connected", offered: "Choose a game folder", sending: "Sending", verifying: "Awaiting verification",
        complete: "Verified · session ended", interrupted: "Transfer interrupted", disconnected: "Disconnected",
        closed: "Session closed", error: "Connection stopped"})[phase] || "Not connected"
    spacing: 8 * unit
    RowLayout {
        Layout.fillWidth: true; spacing: 9 * panel.unit
        Image { source: panel.hostKind === "deck" ? "../assets/handheld.svg" : panel.hostKind === "pi" ? "../assets/pi-board.svg" : "../assets/computer.svg"; Layout.preferredWidth: 48 * panel.unit; Layout.preferredHeight: 38 * panel.unit }
        ColumnLayout {
            Layout.fillWidth: true; Layout.preferredWidth: 150; spacing: 1
            Text { Layout.fillWidth: true; text: panel.hostKind === "deck" ? "This Steam Deck" : panel.hostKind === "pi" ? "This Raspberry Pi" : "This computer"; color: "#f2f5e8"; font.bold: true; font.pixelSize: 20 * panel.unit; elide: Text.ElideRight }
            Text { Layout.fillWidth: true; text: panel.info.address ? panel.info.address.split(":")[0] : "Game source"; color: "#b9c9bd"; font.pixelSize: 16 * panel.unit; elide: Text.ElideRight }
        }
        Rectangle { Layout.preferredWidth: panel.width > 620 ? 48 : 18; height: 2; color: panel.linked ? panel.stateColor : "#829583" }
        Image { source: "../assets/handheld.svg"; opacity: panel.info.peer || panel.info.device ? 1 : 0.52; Layout.preferredWidth: 52 * panel.unit; Layout.preferredHeight: 40 * panel.unit }
        ColumnLayout {
            Layout.fillWidth: true; Layout.preferredWidth: 185; spacing: 1
            Text { Layout.fillWidth: true; text: panel.info.device || (panel.info.peer ? "Receiver · " + panel.info.peer : "Handheld"); textFormat: Text.PlainText; elide: Text.ElideRight; color: "#f2f5e8"; font.bold: true; font.pixelSize: 20 * panel.unit }
            RowLayout {
                Layout.fillWidth: true; spacing: 5 * panel.unit
                Rectangle {
                    Layout.preferredWidth: 8 * panel.unit; Layout.preferredHeight: 8 * panel.unit; radius: width / 2
                    border.width: 1; border.color: panel.stateColor
                    color: panel.linked ? panel.stateColor : "transparent"
                }
                Text { Layout.fillWidth: true; text: panel.phaseText; elide: Text.ElideRight; color: panel.stateColor; font.pixelSize: 16 * panel.unit }
            }
        }
    }
    ColumnLayout {
        visible: !!panel.info.filename && ["sending", "verifying", "interrupted"].includes(panel.phase)
        Layout.fillWidth: true; spacing: 4 * panel.unit
        RowLayout {
            Layout.fillWidth: true
            Text { Layout.fillWidth: true; text: panel.info.filename || ""; textFormat: Text.PlainText; elide: Text.ElideMiddle; color: "#eff2e9"; font.pixelSize: 18 * panel.unit }
            Text { text: panel.info.amount || ""; color: "#c9d4c8"; font.pixelSize: 16 * panel.unit }
            Text { visible: text.length > 0; text: panel.info.speed || ""; color: "#dcf5a9"; font.bold: true; font.pixelSize: 18 * panel.unit }
        }
        Rectangle {
            Layout.fillWidth: true; implicitHeight: 5 * panel.unit; radius: height / 2; color: "#4d6255"
            Rectangle { height: parent.height; width: parent.width * Math.max(0, Math.min(1, panel.info.fraction || 0)); radius: parent.radius; color: "#d9ef9b" }
        }
    }
}
