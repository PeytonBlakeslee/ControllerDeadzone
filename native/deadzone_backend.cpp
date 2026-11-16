// Borderlands 2 Controller Deadzone Control: Win32 WILLOW native radial backend.
// Native game-thread hooks plus Python configuration and verified recovery.
// Physical source writes use compare-before-write ownership; axial math is BL2's.
#include "input_state.hpp"
#include <array>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <exception>
#include <stdexcept>
#include <string>
#include <algorithm>
#include <map>
#include <tuple>
#include <pybind11/pybind11.h>
#include "unrealsdk/unreal/classes/uobject_funcs.h"
#include "unrealsdk/unreal/classes/uclass.h"
#include "unrealsdk/unreal/properties/copyable_property.h"
#include "unrealsdk/unreal/structs/fname.h"
#include "unrealsdk/hook_manager.h"
#include "unrealsdk/unreal/wrappers/wrapped_struct.h"
#include "unrealsdk/unreal/properties/zboolproperty.h"
#include "unrealsdk/unreal/properties/zbyteproperty.h"
#include "unrealsdk/unreal/properties/zarrayproperty.h"
#include "unrealsdk/unreal/properties/zstructproperty.h"
#include "unrealsdk/unreal/properties/zobjectproperty.h"

namespace py = pybind11;
namespace u = unrealsdk::unreal;
namespace {
constexpr int FIELDS = 9;
// Called only from Python-facing registration functions while holding the GIL.
// Each loaded native module obtains its own CSPRNG namespace and never reuses
// an identifier for a subsequent attempt. Hook callbacks do no extra work.
std::wstring next_hook_id(const wchar_t* label) {
 static const std::wstring nonce = [] {
   const auto text = py::module_::import("secrets").attr("token_hex")(16).cast<std::string>();
   if (text.size() != 32) throw std::runtime_error("invalid hook nonce");
   return std::wstring(text.begin(), text.end());
 }();
 static std::uint64_t sequence = 0;
 if (sequence == UINT64_MAX) throw std::runtime_error("hook identifier sequence exhausted");
 return std::wstring(label) + L"/" + nonce + L"/" + std::to_wstring(++sequence);
}
double monotonic_seconds() noexcept {
 using Clock=std::chrono::steady_clock;
 return std::chrono::duration<double>(Clock::now().time_since_epoch()).count();
}
#include "deadzone_native_event_cache.inc"
#include "deadzone_native_radial.inc"
Backend backend{};
#include "deadzone_native_axis.inc"
#include "deadzone_native_key.inc"
#include "deadzone_native_preprocess.inc"
#include "deadzone_native_fields.inc"
#include "deadzone_native_discovery.inc"
#include "deadzone_native_lifecycle.inc"
}
PYBIND11_MODULE(deadzone_backend, mod) {
    mod.doc()="Controller Deadzone Control paired radial filter, Win32";
    mod.def("backend_version", [](){return "1.0.5";});
    mod.def("start_lifecycle_hooks",&lifecycle::start);
    mod.def("stop_lifecycle_hooks",&lifecycle::stop);
    mod.def("lifecycle_begin",&lifecycle::begin);
    mod.def("lifecycle_resume",&lifecycle::resume);
    mod.def("lifecycle_active",&lifecycle::active);
    mod.def("scan_controller_bindings",&binding_discovery::scan);
    mod.def("read_axis_field",&axis_fields::read);
    mod.def("cas_axis_field",&axis_fields::cas);
    mod.def("restore_owned_axis_field",&axis_fields::restore_owned);
    mod.def("relinquish_owned_axis_field",&axis_fields::relinquish);
    mod.def("clear_owned_axis_fields",&axis_fields::clear_owners);
    mod.def("start_axis_hook",&axis_hook::start);
    mod.def("stop_axis_hook",&axis_hook::stop);
    mod.def("axis_hook_active",&axis_hook::active);
    mod.def("start_key_hook",&key_hook::start);
    mod.def("stop_key_hook",&key_hook::stop);
    mod.def("key_hook_active",&key_hook::active);
    mod.def("start_preprocess_hook",&preprocess_hook::start);
    mod.def("stop_preprocess_hook",&preprocess_hook::stop);
    mod.def("suspend_preprocess_hook",&preprocess_hook::suspend);
    mod.def("preprocess_hook_active",&preprocess_hook::active);
    mod.def("configure_input",[](bool left,bool right,bool priority,bool protect,
                                   double wake,double li,double ri){
        backend.input.configure(left,right,priority,protect,wake,li,ri);
    });
    mod.def("reset_input",[](){backend.input.reset();});
    mod.def("axis_event",[](int index,double delta,bool gamepad){
        return backend.input.axis(index,delta,gamepad,monotonic_seconds());
    });
    mod.def("key_event",[](const std::string& name,bool gamepad,int kind){
        return backend.input.key(name,gamepad,kind,monotonic_seconds());
    });
    mod.def("input_owner",[](){return static_cast<int>(backend.input.owner);});
    mod.def("guard_targets",[](){return backend.input.guard(monotonic_seconds());});
    mod.def("apply_cached_radial",[](std::uintptr_t address,bool using_gamepad,
                                     bool left,bool right,
                                     double li,double lo,double ri,double ro){
        const double now=monotonic_seconds();
        if(!backend.input.frame_allowed(using_gamepad,now))return 2;
        const auto lx=left?backend.input.sample(0,now):0.0;
        const auto ly=left?backend.input.sample(1,now):0.0;
        const auto rx=right?backend.input.sample(2,now):0.0;
        const auto ry=right?backend.input.sample(3,now):0.0;
        return backend.apply(address,left,right,lx,ly,rx,ry,li,lo,ri,ro);
    });
    mod.def("apply_radial", [](std::uintptr_t addr, bool left, bool right,
                               double lx, double ly, double rx, double ry,
                               double li, double lo, double ri, double ro) {
        return backend.apply(addr,left,right,lx,ly,rx,ry,li,lo,ri,ro);
    });
    mod.def("reset", [](){backend.reset();});
}
