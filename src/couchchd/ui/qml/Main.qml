import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ApplicationWindow {
    id: window
    visible: true
    width: Math.min(1280, Screen.desktopAvailableWidth)
    height: Math.min(800, Screen.desktopAvailableHeight)
    minimumWidth: 640; minimumHeight: 480
    title: "CouchCHD"
    color: "#18211e"
    font.family: "sans-serif"
    readonly property real u: width >= 1600 && height >= 900 ? 1.22 : 1
    readonly property real marginX: width >= 1050 ? 36 * u : 16 * u
    readonly property bool compact: width < 1050
    readonly property bool inDialog: library.modal.kind !== ""
    readonly property bool showingSystems: library.section === 1 && !library.queueVisible
    readonly property var focusedSystem: showingSystems ? library.rows[mainIndex] : null
    property int mainIndex: 0
    property int dialogIndex: 0
    property bool closingApproved: false
    onClosing: function(event) {
        if (!closingApproved) { event.accepted = false; library.exitPrompt() }
    }
    function selectIndex(list, index) {
        if (list.count === 0) return
        let target = Math.max(0, Math.min(list.count - 1, index))
        if (list === dialogList) dialogIndex = target
        else mainIndex = target
        list.positionViewAtIndex(target, ListView.Contain)
    }
    function route(action) {
        let list = inDialog ? dialogList : showingSystems ? systemGrid : mainList
        let index = inDialog ? dialogIndex : mainIndex
        if (library.queueVisible && !inDialog) {
            if (action === "back") library.back()
            else if (!progressView.navigate(action)) return
            library.cue("tick")
            return
        }
        if (library.busy && !(inDialog && (library.modal.kind === "confirm" || library.modal.kind === "item")) && action !== "back") return
        switch (action) {
        case "up": case "down":
            selectIndex(list, index + (action === "up" ? -1 : 1) * (list === systemGrid ? systemGrid.columns : 1)); break
        case "left": case "right":
            if (inDialog && bodyScroll.contentHeight > bodyScroll.height + 1) {
                bodyScroll.contentY = Math.max(0, Math.min(bodyScroll.contentHeight - bodyScroll.height,
                        bodyScroll.contentY + (action === "left" ? -1 : 1) * Math.max(32, bodyScroll.height * .8)))
            } else selectIndex(list, index + (action === "left" ? -1 : 1) * (list === systemGrid ? 1 : 4))
            break
        case "confirm": library.activate(index, false); break
        case "inspect": if (!inDialog) library.activate(index, true); break
        case "primary": library.primary(); break
        case "back": library.back(); break
        case "settings": library.settings(); break
        case "previous": library.switchTab(library.section - 1); break
        case "next": library.switchTab(library.section + 1); break
        }
        library.cue("tick")
    }
    Connections {
        target: router
        function onAction(action) { window.route(action) }
    }
    Connections {
        target: library
        function onSceneChanged(reset) {
            if (reset) {
                if (window.inDialog) { window.dialogIndex = 0; bodyScroll.contentY = 0 }
                else window.mainIndex = 0
            }
            Qt.callLater(function() {
                let list = window.inDialog ? dialogList : window.showingSystems ? systemGrid : mainList
                window.selectIndex(list, window.inDialog ? window.dialogIndex : window.mainIndex)
            })
        }
        function onQuitRequested() { window.closingApproved = true; window.close() }
    }
    ColumnLayout {
        anchors.fill: parent; spacing: 0
        enabled: !window.inDialog
        Rectangle {
            Layout.fillWidth: true; Layout.preferredHeight: 66 * window.u; color: "#202b26"
            RowLayout {
                anchors.fill: parent; anchors.leftMargin: window.marginX; anchors.rightMargin: window.marginX
                spacing: 12 * window.u
                Image { visible: !library.queueVisible; Layout.preferredWidth: 38 * window.u; Layout.preferredHeight: 38 * window.u; source: "../assets/mark.svg"; sourceSize: Qt.size(88,88) }
                Text { text: "CouchCHD"; color: "#eff2e9"; font.pixelSize: 29 * window.u; font.weight: Font.Bold; font.letterSpacing: -1 }
                Rectangle { Layout.preferredWidth: 1; Layout.preferredHeight: 25 * window.u; color: "#516052" }
                Text {
                    Layout.fillWidth: true; text: library.location; textFormat: Text.PlainText
                    elide: Text.ElideMiddle; color: "#c0caba"; font.pixelSize: 21 * window.u
                }
                Text { visible: library.queueVisible; text: library.space + " free in library"; color: "#d3dbcf"; font.pixelSize: 21 * window.u }
                Command { visible: !library.queueVisible; label: "Settings"; glyph: router.glyphs[4]; compact: true; unit: window.u; enabled: !library.busy; onActivated: window.route("settings") }
            }
        }
        Rectangle {
            visible: !library.queueVisible
            Layout.fillWidth: true; Layout.preferredHeight: 55 * window.u; color: "#202b26"
            RowLayout {
                anchors.fill: parent; anchors.leftMargin: window.marginX; anchors.rightMargin: window.marginX; spacing: 6 * window.u
                Repeater {
                    model: library.sections
                    delegate: Rectangle {
                        id: tab
                        required property string modelData
                        required property int index
                        Layout.fillWidth: true; Layout.fillHeight: true
                        Layout.preferredWidth: tabTitle.implicitWidth + 10 * window.u
                        color: tabHover.hovered ? "#303d34" : "transparent"
                        Accessible.role: Accessible.PageTab; Accessible.name: modelData; Accessible.selected: index === library.section
                        Accessible.onPressAction: library.switchTab(index)
                        HoverHandler { id: tabHover }
                        TapHandler { onTapped: library.switchTab(tab.index) }
                        Text {
                            id: tabTitle; anchors.fill: parent; verticalAlignment: Text.AlignVCenter; horizontalAlignment: Text.AlignHCenter
                            text: window.compact ? ["Dashboard", "Systems", "M3U", "Libraries", "History"][tab.index] : tab.modelData
                            elide: Text.ElideRight; font.pixelSize: 22 * window.u
                            color: tab.index === library.section ? "#e1f3b4" : "#b7c4b8"
                            font.weight: tab.index === library.section ? Font.DemiBold : Font.Normal
                        }
                        Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 3 * window.u; color: "#cee997"; visible: tab.index === library.section }
                    }
                }
            }
        }
        ColumnLayout {
            visible: !library.queueVisible
            Layout.fillWidth: true; Layout.fillHeight: true
            Layout.leftMargin: window.marginX; Layout.rightMargin: window.marginX
            Layout.topMargin: window.height < 700 ? 12 : 25 * window.u
            spacing: 9 * window.u
            RowLayout {
                Layout.fillWidth: true; spacing: 22 * window.u
                Text {
                    Layout.fillWidth: true; color: "#f3f4eb"
                    text: library.sections[library.section]; font.pixelSize: (window.compact ? 35 : 43) * window.u; font.weight: Font.DemiBold; font.letterSpacing: -1
                    elide: Text.ElideRight
                }
                Command { label: library.primaryLabel; glyph: router.glyphs[3]; unit: window.u; filled: true; enabled: !library.busy; onActivated: window.route("primary") }
            }
            Text {
                Layout.fillWidth: true; visible: window.height >= 620
                color: "#b7c5b9"; text: library.subtitle; textFormat: Text.PlainText
                font.pixelSize: 24 * window.u; wrapMode: Text.Wrap
            }
            RowLayout {
                visible: !window.showingSystems || window.height >= 620
                Layout.fillWidth: true; Layout.topMargin: 7 * window.u; Layout.bottomMargin: 4 * window.u; spacing: 22 * window.u
                Text { text: library.summary; color: "#d9ebbe"; font.pixelSize: 21 * window.u; font.weight: Font.DemiBold }
                Item { Layout.fillWidth: true }
                Text { visible: library.space !== "—" && library.section !== 3; text: library.space + " free in library"; color: "#d3dbcf"; font.pixelSize: 21 * window.u }
                Text { visible: library.savings !== "—" && library.section !== 3 && (!window.compact || window.width >= 840); text: "Compression difference: " + library.savings; color: "#d3dbcf"; font.pixelSize: 21 * window.u }
            }
            Rectangle {
                Layout.fillWidth: true; Layout.fillHeight: true; Layout.minimumHeight: 40
                radius: 7 * window.u; color: "#253029"
                ListView {
                    id: mainList; anchors.fill: parent; anchors.margins: 6 * window.u
                    visible: !window.showingSystems
                    clip: true; model: visible && !library.queueVisible ? library.rows : null; spacing: 2 * window.u; currentIndex: window.mainIndex
                    boundsBehavior: Flickable.StopAtBounds; keyNavigationEnabled: false
                    ScrollBar.vertical: ScrollBar {}
                    delegate: Entry {
                        required property var modelData; required property int index
                        width: mainList.width - 10 * window.u; height: implicitHeight; unit: window.u
                        entry: modelData; selected: index === window.mainIndex && !window.inDialog
                        onActivated: { window.mainIndex = index; window.route("confirm") }
                    }
                }
                GridView {
                    id: systemGrid; anchors.fill: parent; anchors.margins: 3 * window.u
                    visible: window.showingSystems; clip: true; model: visible ? library.rows : null
                    readonly property int columns: Math.max(2, Math.floor(width / (182 * window.u)))
                    cellWidth: width / columns; cellHeight: (window.height < 620 ? 80 : 102) * window.u
                    currentIndex: window.mainIndex; keyNavigationEnabled: false; boundsBehavior: Flickable.StopAtBounds
                    ScrollBar.vertical: ScrollBar {}
                    delegate: SystemTile {
                        required property var modelData; required property int index
                        width: systemGrid.cellWidth; height: systemGrid.cellHeight; unit: window.u
                        entry: modelData; selected: index === window.mainIndex && !window.inDialog
                        onActivated: { window.mainIndex = index; window.route("confirm") }
                    }
                }
            }
            RowLayout {
                Layout.fillWidth: true; Layout.preferredHeight: 42 * window.u
                Rectangle { width: 10 * window.u; height: width; radius: width / 2; color: library.busy ? "#ffd188" : "#b6d588" }
                Text {
                    Layout.fillWidth: true; text: window.focusedSystem && !library.busy
                        ? window.focusedSystem.label + " · " + window.focusedSystem.detail : library.status; textFormat: Text.PlainText
                    color: "#c4d0c2"; font.pixelSize: 20 * window.u; elide: Text.ElideMiddle
                    Accessible.role: Accessible.StaticText
                }
            }
        }
        QueueView {
            id: progressView
            Layout.fillHeight: true; Layout.fillWidth: true
            visible: library.queueVisible; unit: window.u; gutter: window.marginX; dialogOpen: window.inDialog
        }
        Rectangle {
            visible: !library.queueVisible
            Layout.fillWidth: true; Layout.preferredHeight: 51 * window.u; color: "#111a16"
            RowLayout {
                anchors.fill: parent; anchors.leftMargin: window.marginX; anchors.rightMargin: window.marginX; spacing: 12 * window.u
                Command { compact: true; unit: window.u; label: "Select"; glyph: router.glyphs[0]; onActivated: window.route("confirm") }
                Command { compact: true; unit: window.u; label: library.operation === "Compressing discs" ? "Cancel" : "Back"; glyph: router.glyphs[1]; onActivated: window.route("back") }
                Command { compact: true; unit: window.u; label: "Inspect"; glyph: router.glyphs[2]; onActivated: window.route("inspect") }
                Command { compact: true; unit: window.u; visible: !window.compact; label: library.primaryLabel; glyph: router.glyphs[3]; onActivated: window.route("primary") }
                Item { Layout.fillWidth: true }
                Command { compact: true; unit: window.u; label: "Tabs"; glyph: router.glyphs[5]; onActivated: window.route("next") }
            }
        }
    }
    // No Popup, Dialog or native file picker: every interaction belongs to this surface.
    Rectangle {
        id: scrim; anchors.fill: parent; visible: window.inDialog; color: "#c4101612"
        MouseArea { anchors.fill: parent; onClicked: function(mouse) { mouse.accepted = true } }
        Rectangle {
            anchors.centerIn: parent; width: Math.min(parent.width - 32 * window.u, 880 * window.u)
            height: Math.min(parent.height - 38 * window.u, 660 * window.u)
            radius: 12 * window.u; color: "#26332b"; border.color: "#697967"; border.width: 1
            ColumnLayout {
                anchors.fill: parent; anchors.margins: 24 * window.u; spacing: 12 * window.u
                RowLayout {
                    Layout.fillWidth: true
                    Text {
                        Layout.fillWidth: true; text: library.modal.title; textFormat: Text.PlainText; elide: Text.ElideMiddle
                        font.pixelSize: 33 * window.u; font.weight: Font.DemiBold; color: "#f2f5e8"
                    }
                    Command { label: "Back"; glyph: router.glyphs[1]; compact: true; unit: window.u; onActivated: window.route("back") }
                }
                TransferLink {
                    visible: !!library.transferView.phase; Layout.fillWidth: true
                    info: library.transferView; hostKind: library.hostKind; unit: window.u
                }
                Flickable {
                    id: bodyScroll; Layout.fillWidth: true
                    Layout.preferredHeight: Math.min(bodyText.implicitHeight + 3, window.height * .24)
                    contentWidth: width; contentHeight: bodyText.implicitHeight + 3
                    clip: true; boundsBehavior: Flickable.StopAtBounds
                    ScrollBar.vertical: ScrollBar {}
                    Text {
                        id: bodyText; width: Math.max(1, bodyScroll.width - 16); text: library.modal.body; textFormat: Text.PlainText
                        color: "#ced8c9"; font.pixelSize: 24 * window.u; wrapMode: Text.Wrap
                    }
                }
                Text { visible: bodyScroll.contentHeight > bodyScroll.height + 1; text: "Left / right to scroll details"; color: "#d1deb9"; font.pixelSize: 20 * window.u }
                Rectangle { Layout.fillWidth: true; height: 1; color: "#526152" }
                ListView {
                    id: dialogList; Layout.fillWidth: true; Layout.fillHeight: true
                    clip: true; model: library.modalRows; spacing: 2 * window.u
                    boundsBehavior: Flickable.StopAtBounds; keyNavigationEnabled: false; currentIndex: window.dialogIndex
                    ScrollBar.vertical: ScrollBar {}
                    delegate: Entry {
                        required property var modelData; required property int index
                        width: dialogList.width - 10 * window.u; height: implicitHeight; unit: window.u
                        entry: modelData; selected: index === window.dialogIndex; expanded: true
                        onActivated: { window.dialogIndex = index; window.route("confirm") }
                    }
                }
                RowLayout {
                    Layout.fillWidth: true
                    Command { compact: true; label: "Select"; glyph: router.glyphs[0]; unit: window.u; onActivated: window.route("confirm") }
                    Item { Layout.fillWidth: true }
                    Text { text: library.busy ? library.status : router.device; elide: Text.ElideMiddle; Layout.maximumWidth: dialogList.width * .65; textFormat: Text.PlainText; color: "#becbb9"; font.pixelSize: 20 * window.u }
                }
            }
        }
    }
}
