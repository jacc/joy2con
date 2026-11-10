// SPDX-License-Identifier: GPL-3.0-or-later
// One Joy-Con: live drawing, status, motion, tests and identity.
import QtQuick
import QtQuick.Layouts
import QtQuick.Controls as QQC2
import org.kde.kirigami as Kirigami
import org.kde.kirigamiaddons.formcard as FormCard

FormCard.FormCardPage {
    id: page
    required property string side
    readonly property var st: backend.state
    readonly property var jc: (st.joycons || {})[side]
    readonly property bool live: !!jc
    readonly property var known: {
        const k = st.known || {};
        for (const mac in k) if (k[mac].side === side) return k[mac];
        return null;
    }
    readonly property var info: live ? jc.info : (known || {})
    readonly property var colors: info.colors || {}
    readonly property bool charging: live && jc.charge > 0 && jc.charge !== 0x20
    title: side === "left" ? "Left Joy-Con" : "Right Joy-Con"

    component ActionRow: FormCard.FormButtonDelegate {
        trailingLogo.visible: false
        enabled: page.live
    }

    JoyConArt {
        Layout.alignment: Qt.AlignHCenter
        Layout.topMargin: Kirigami.Units.gridUnit
        Layout.preferredWidth: Kirigami.Units.gridUnit * 7
        Layout.preferredHeight: Layout.preferredWidth * 440 / 160
        side: page.side
        accent: page.colors.highlight || (page.side === "left" ? "#9be1e6" : "#ff8c5f")
        buttonsDown: page.live ? page.jc.buttons : []
        stick: page.live ? page.jc.stick : [0, 0]
        playerLeds: {
            const e = page.st.emulated || {};
            const d = e.pro || e[page.side];
            return d ? d.player_leds : 0;
        }
        live: page.live
        opacity: page.live ? 1 : 0.35
    }

    FormCard.FormHeader { title: "Status" }
    FormCard.FormCard {
        FormCard.FormTextDelegate {
            text: "Connection"
            description: !page.live ? (page.known ? "Asleep. Press any button on it to reconnect." : "Not paired with this PC.")
                         : page.jc.ready ? "Connected and paired with this PC" : "Connecting"
        }
        FormCard.FormDelegateSeparator {}
        FormCard.FormTextDelegate {
            text: "Battery"
            visible: page.live
            description: !page.live ? "" : (page.jc.battery_mv / 1000).toFixed(2) + " V, "
                         + (page.jc.charge === 0x20 ? "fully charged" : page.charging ? "charging" : "not charging")
            trailing: QQC2.Label {
                text: page.live ? page.jc.battery_pct + "%" : ""
                color: page.live && page.jc.battery_pct < 15 && !page.charging ? Kirigami.Theme.negativeTextColor : Kirigami.Theme.textColor
                font.features: { "tnum": 1 }
            }
        }
        FormCard.FormDelegateSeparator { visible: page.live }
        FormCard.FormTextDelegate {
            text: "Input"
            visible: page.live
            readonly property bool slow: page.live && page.jc.rate_hz > 0 && page.jc.rate_hz < 100
            readonly property var other: (page.st.joycons || {})[page.side === "left" ? "right" : "left"]
            // Two links whose rates add up to one full-rate link: the adapter
            // is taking turns between them.
            readonly property bool sharing: slow && !!other && other.rate_hz < 100
                                            && page.jc.rate_hz + other.rate_hz > 110 && page.jc.rate_hz + other.rate_hz < 150
            description: !page.live ? ""
                         : sharing ? "Your Bluetooth adapter serves one Joy-Con per 7.5 ms, so with both connected they take turns."
                         : slow ? "Below the 133 per second a Joy-Con can send. Another Bluetooth device or a device search may be busy on the radio."
                         : "Longest gap in the last two seconds: " + page.jc.worst_gap_ms.toFixed(0) + " ms"
            trailing: QQC2.Label {
                text: page.live ? page.jc.rate_hz.toFixed(0) + " per second" : ""
                font.features: { "tnum": 1 }
            }
        }
    }

    FormCard.FormHeader { title: "Motion" }
    FormCard.FormCard {
        GyroGraph {
            Layout.fillWidth: true
            Layout.margins: Kirigami.Units.largeSpacing
            jc: page.jc
        }
        FormCard.FormDelegateSeparator {}
        ActionRow {
            text: "Recenter"
            description: "Takes the current direction as straight ahead."
            onClicked: backend.action("reset_orientation", {})
        }
    }

    FormCard.FormHeader { title: "Test" }
    FormCard.FormCard {
        ActionRow {
            text: "Rumble"
            onClicked: backend.action("rumble", { side: page.side, amp: 0.4, seconds: 0.35 })
        }
        FormCard.FormDelegateSeparator {}
        ActionRow {
            text: "Play a sound"
            description: "Handy for finding it."
            onClicked: backend.action("sample", { side: page.side, id: 2 })
        }
    }

    FormCard.FormHeader { title: "Details" }
    FormCard.FormCard {
        FormCard.FormTextDelegate { text: "Serial number"; description: page.info.serial || "Unknown" }
        FormCard.FormDelegateSeparator {}
        FormCard.FormTextDelegate { text: "Firmware"; description: page.info.firmware || "Unknown" }
        FormCard.FormDelegateSeparator {}
        FormCard.FormTextDelegate { text: "Bluetooth address"; description: page.live ? page.jc.mac : (page.known ? page.known.mac || "Unknown" : "Unknown") }
    }

    FormCard.FormCard {
        Layout.topMargin: Kirigami.Units.gridUnit
        ActionRow {
            text: "Disconnect"
            description: "It reconnects when you press one of its buttons."
            onClicked: backend.action("disconnect", { side: page.side })
        }
    }
    Item { Layout.preferredHeight: Kirigami.Units.gridUnit }
}
