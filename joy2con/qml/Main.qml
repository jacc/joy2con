// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Layouts
import QtQuick.Controls as QQC2
import org.kde.kirigami as Kirigami
import org.kde.kirigamiaddons.formcard as FormCard

Kirigami.ApplicationWindow {
    id: root
    title: "joy2con"
    width: Kirigami.Units.gridUnit * 32
    height: Kirigami.Units.gridUnit * 46
    minimumWidth: Kirigami.Units.gridUnit * 22
    minimumHeight: Kirigami.Units.gridUnit * 30

    readonly property var st: backend.state
    readonly property var joycons: st.joycons || ({})
    readonly property var emulated: st.emulated || ({})
    readonly property var knownBySide: {
        const out = {};
        const k = st.known || {};
        for (const mac in k) out[k[mac].side] = k[mac];
        return out;
    }
    readonly property var otherHost: (st.seen || []).filter(s => s.kind === "other-host")

    function ledsFor(side) {
        const e = emulated.pro || emulated[side];
        return e ? e.player_leds : 0;
    }
    function sideName(side) { return side === "left" ? "Left Joy-Con" : "Right Joy-Con"; }
    function openJoyCon(side) {
        pageStack.pop(home);
        pageStack.push(Qt.resolvedUrl("JoyConPage.qml"), { side: side });
    }
    function openDiagnostics() {
        pageStack.pop(home);
        pageStack.push(Qt.resolvedUrl("DiagnosticsPage.qml"));
    }
    function consumers() {
        const names = [];
        for (const key in emulated)
            for (const c of emulated[key].consumers)
                if (names.indexOf(c.name) < 0) names.push(c.name);
        return names;
    }

    onClosing: (close) => { close.accepted = false; root.hide(); }

    pageStack.globalToolBar.style: Kirigami.ApplicationHeaderStyle.ToolBar
    pageStack.globalToolBar.showNavigationButtons: Kirigami.ApplicationHeaderStyle.ShowBackButton
    pageStack.columnView.columnResizeMode: Kirigami.ColumnView.SingleColumn
    pageStack.initialPage: FormCard.FormCardPage {
        id: home
        title: "Joy-Cons"
        actions: [
            Kirigami.Action {
                text: "Diagnostics"
                icon.name: "view-list-details"
                onTriggered: root.openDiagnostics()
            }
        ]

        component Banner: Kirigami.InlineMessage {
            Layout.fillWidth: true
            Layout.maximumWidth: Kirigami.Units.gridUnit * 30
            Layout.alignment: Qt.AlignHCenter
            Layout.topMargin: Kirigami.Units.largeSpacing
            Layout.leftMargin: Kirigami.Units.largeSpacing
            Layout.rightMargin: Kirigami.Units.largeSpacing
        }

        Banner {
            type: Kirigami.MessageType.Error
            visible: !backend.connected
            text: "The joy2con driver isn't running."
            actions: Kirigami.Action { text: "Start"; onTriggered: backend.startDriver() }
        }
        Banner {
            type: Kirigami.MessageType.Warning
            visible: backend.connected && !!root.st.foreign_discovery
            text: "Another program is searching for Bluetooth devices, so Joy-Cons can't connect and lose input. Close the Bluetooth \"Add device\" window."
        }
        Repeater {
            model: root.otherHost
            delegate: Banner {
                required property var modelData
                visible: true
                type: Kirigami.MessageType.Information
                text: "A " + root.sideName(modelData.side) + " paired with a Switch is nearby."
                actions: Kirigami.Action {
                    text: "Use with this PC"
                    onTriggered: backend.action("takeover", { mac: modelData.mac })
                }
            }
        }

        PairView {
            Layout.alignment: Qt.AlignHCenter
            Layout.topMargin: Kirigami.Units.gridUnit * 1.5
            joycons: root.joycons
            known: root.knownBySide
            ledsFor: root.ledsFor
            highlightSide: leftRow.hovered ? "left" : rightRow.hovered ? "right" : ""
            onOpen: (side) => root.openJoyCon(side)
        }
        QQC2.Label {
            Layout.alignment: Qt.AlignHCenter
            Layout.maximumWidth: Kirigami.Units.gridUnit * 20
            Layout.topMargin: Kirigami.Units.largeSpacing
            visible: !(root.joycons.left && root.joycons.right)
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.WordWrap
            color: Kirigami.Theme.disabledTextColor
            text: "Press any button on a Joy-Con to wake it. To pair a new one, press the sync button on its rail."
        }

        FormCard.FormHeader { title: "Controllers" }
        FormCard.FormCard {
            component ControllerRow: FormCard.FormButtonDelegate {
                required property string side
                readonly property var jc: root.joycons[side]
                readonly property var known: root.knownBySide[side]
                text: root.sideName(side)
                description: jc ? (jc.ready ? "Connected" : "Connecting")
                                : known ? "Asleep" : "Not paired"
                onClicked: root.openJoyCon(side)
            }
            ControllerRow { id: leftRow; side: "left" }
            FormCard.FormDelegateSeparator {}
            ControllerRow { id: rightRow; side: "right" }
        }

        FormCard.FormHeader { title: "In games" }
        FormCard.FormCard {
            FormCard.FormComboBoxDelegate {
                id: modeBox
                text: "Appear as"
                model: ["One Pro Controller", "Two separate Joy-Cons"]
                onActivated: (i) => backend.action("mode", { mode: i === 1 ? "joycons" : "pro" })
                Binding { target: modeBox; property: "currentIndex"; value: root.st.mode === "joycons" ? 1 : 0 }
            }
            FormCard.FormDelegateSeparator { visible: imuBox.visible }
            FormCard.FormComboBoxDelegate {
                id: imuBox
                visible: root.st.mode !== "joycons"
                text: "Motion controls from"
                model: ["Right Joy-Con", "Left Joy-Con"]
                onActivated: (i) => backend.action("imu_source", { source: i === 1 ? "left" : "right" })
                Binding { target: imuBox; property: "currentIndex"; value: root.st.imu_source === "left" ? 1 : 0 }
            }
            FormCard.FormDelegateSeparator {}
            FormCard.FormSwitchDelegate {
                id: driftSwitch
                text: "Correct gyro drift"
                description: "Re-zeroes the gyro whenever a Joy-Con lies still."
                onToggled: backend.action("gyro_autocal", { enabled: checked })
                Binding { target: driftSwitch; property: "checked"; value: !!root.st.gyro_autocal }
            }
            FormCard.FormDelegateSeparator {}
            FormCard.FormTextDelegate {
                text: "In use by"
                description: {
                    const names = root.consumers();
                    return names.length ? names.join(", ") : "No program right now";
                }
            }
        }

        Item { Layout.preferredHeight: Kirigami.Units.gridUnit }
    }
}
