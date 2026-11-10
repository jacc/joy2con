// SPDX-License-Identifier: GPL-3.0-or-later
// The virtual controllers the system sees, link statistics and the log.
import QtQuick
import QtQuick.Layouts
import QtQuick.Controls as QQC2
import org.kde.kirigami as Kirigami
import org.kde.kirigamiaddons.formcard as FormCard

FormCard.FormCardPage {
    id: page
    title: "Diagnostics"
    readonly property var st: backend.state
    readonly property var emulated: st.emulated || ({})
    readonly property var joycons: st.joycons || ({})

    FormCard.FormHeader { title: "Virtual controllers" }
    Repeater {
        model: Object.keys(page.emulated)
        delegate: FormCard.FormCard {
            required property string modelData
            readonly property var e: page.emulated[modelData]
            Layout.bottomMargin: Kirigami.Units.largeSpacing
            FormCard.FormTextDelegate {
                text: "Nintendo Switch " + e.name + (e.retiring ? " (being removed)" : "")
                description: {
                    const nodes = (e.nodes.hidraw || []).concat((e.nodes.event || []).map(v => v.node.replace("/dev/input/", "")));
                    return nodes.length ? nodes.join(", ") : "No device nodes yet";
                }
            }
            FormCard.FormDelegateSeparator {}
            FormCard.FormTextDelegate {
                text: "Open in"
                description: e.consumers.length ? e.consumers.map(c => c.name + " (" + c.pid + ")").join(", ") : "No program"
            }
            FormCard.FormDelegateSeparator {}
            FormCard.FormTextDelegate {
                text: "Host settings"
                description: "Report mode 0x" + e.mode + ", motion " + (e.imu ? "on" : "off") + ", rumble " + (e.vibration ? "on" : "off")
            }
            FormCard.FormDelegateSeparator {}
            FormCard.FormTextDelegate {
                text: "Traffic"
                description: e.reports + " reports sent, " + e.subcommands + " commands answered"
            }
        }
    }
    FormCard.FormCard {
        visible: Object.keys(page.emulated).length === 0
        FormCard.FormTextDelegate { text: "None"; description: "One appears when a Joy-Con connects." }
    }

    FormCard.FormHeader { title: "Bluetooth links" }
    FormCard.FormCard {
        Repeater {
            model: ["left", "right"]
            delegate: FormCard.FormTextDelegate {
                required property string modelData
                readonly property var j: page.joycons[modelData]
                text: modelData === "left" ? "Left Joy-Con" : "Right Joy-Con"
                description: !j ? "Not connected"
                    : "Every " + j.interval_ms.toFixed(1) + " ms (jitter " + j.jitter_ms.toFixed(1) + " ms), "
                      + j.gaps + " late reports, " + j.reports + " received"
            }
        }
    }

    FormCard.FormHeader { title: "Log" }
    FormCard.FormCard {
        FormCard.FormTextDelegate { visible: backend.events.length === 0; text: "Nothing logged yet" }
        Repeater {
            model: backend.events.slice(0, 100)
            delegate: FormCard.FormTextDelegate {
                required property var modelData
                text: modelData.text
                description: new Date(modelData.t * 1000).toLocaleTimeString(Qt.locale(), "hh:mm:ss")
                textItem.wrapMode: Text.WordWrap
                textItem.color: modelData.level === "warn" ? Kirigami.Theme.neutralTextColor : Kirigami.Theme.textColor
            }
        }
    }
    Item { Layout.preferredHeight: Kirigami.Units.gridUnit }
}
