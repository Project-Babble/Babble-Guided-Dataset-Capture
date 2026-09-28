#!/usr/bin/env bash
set -euo pipefail

media_dev="${MEDIA_DEV:-/dev/media0}"
fmt="${FORMAT:-SBGGR8_1X8/400x400 field:none}"

primary_sensor="${PRIMARY_SENSOR_ENTITY_NAME:-sf-ov6211-pri 0-0061}"
primary_csiphy="${PRIMARY_CSIPHY_ENTITY_NAME:-msm_csiphy3}"
primary_csid="${PRIMARY_CSID_ENTITY_NAME:-msm_csid3}"
primary_csid_source_pad="${PRIMARY_CSID_SOURCE_PAD:-1}"
primary_rdi="${PRIMARY_RDI_ENTITY_NAME:-msm_vfe3_rdi0}"

secondary_sensor="${SECONDARY_SENSOR_ENTITY_NAME:-sf-ov6211-sec 0-0060}"
secondary_csiphy="${SECONDARY_CSIPHY_ENTITY_NAME:-msm_csiphy5}"
secondary_csid="${SECONDARY_CSID_ENTITY_NAME:-msm_csid4}"
secondary_csid_source_pad="${SECONDARY_CSID_SOURCE_PAD:-1}"
secondary_rdi="${SECONDARY_RDI_ENTITY_NAME:-msm_vfe4_rdi0}"

disable_link() {
  media-ctl -d "${media_dev}" -l "$1 [0]" >/dev/null 2>&1 || true
}

enable_link() {
  media-ctl -d "${media_dev}" -l "$1 [1]"
}

set_fmt() {
  media-ctl -d "${media_dev}" -V "$1 [fmt:${fmt}]" >/dev/null 2>&1 || true
}

case "${1:-on}" in
  off|disable)
    disable_link '"msm_csid3":1 -> "msm_vfe3_rdi0":0'
    disable_link '"msm_csid3":2 -> "msm_vfe3_rdi1":0'
    disable_link '"msm_csid3":3 -> "msm_vfe3_rdi2":0'
    disable_link '"msm_csiphy3":1 -> "msm_csid3":0'
    disable_link '"sf-ov6211-pri 0-0061":0 -> "msm_csiphy3":0'

    disable_link '"msm_csid4":1 -> "msm_vfe4_rdi0":0'
    disable_link '"msm_csid4":2 -> "msm_vfe4_rdi1":0'
    disable_link '"msm_csid4":3 -> "msm_vfe4_rdi2":0'
    disable_link '"msm_csiphy5":1 -> "msm_csid4":0'
    disable_link '"sf-ov6211-sec 0-0060":0 -> "msm_csiphy5":0'
    echo "disabled lower dual VCINT routes"
    exit 0
    ;;
  on|enable)
    ;;
  *)
    echo "usage: $0 [on|off]" >&2
    exit 2
    ;;
esac

if [[ "${SKIP_VCINT_CHECK:-0}" != "1" ]] && [[ -x /usr/lib/deckard-fpga/read_reg ]]; then
  fpga_state="$(/usr/lib/deckard-fpga/read_reg 0x12 2>/dev/null | awk '{print $NF}')"
  if [[ "${fpga_state}" != "0x00021211" ]]; then
    echo "refusing lower dual route: FPGA is ${fpga_state:-unknown}, expected VCINT 0x00021211" >&2
    exit 1
  fi
fi

"$0" off >/dev/null

enable_link "\"${primary_sensor}\":0 -> \"${primary_csiphy}\":0"
enable_link "\"${primary_csiphy}\":1 -> \"${primary_csid}\":0"
enable_link "\"${primary_csid}\":${primary_csid_source_pad} -> \"${primary_rdi}\":0"

enable_link "\"${secondary_sensor}\":0 -> \"${secondary_csiphy}\":0"
enable_link "\"${secondary_csiphy}\":1 -> \"${secondary_csid}\":0"
enable_link "\"${secondary_csid}\":${secondary_csid_source_pad} -> \"${secondary_rdi}\":0"

set_fmt "\"${primary_sensor}\":0"
set_fmt "\"${primary_csiphy}\":0"
set_fmt "\"${primary_csiphy}\":1"
set_fmt "\"${primary_csid}\":0"
set_fmt "\"${primary_csid}\":${primary_csid_source_pad}"
set_fmt "\"${primary_rdi}\":0"

set_fmt "\"${secondary_sensor}\":0"
set_fmt "\"${secondary_csiphy}\":0"
set_fmt "\"${secondary_csiphy}\":1"
set_fmt "\"${secondary_csid}\":0"
set_fmt "\"${secondary_csid}\":${secondary_csid_source_pad}"
set_fmt "\"${secondary_rdi}\":0"

echo "selected lower dual VCINT route: primary=/dev/video9 secondary=/dev/video13"
