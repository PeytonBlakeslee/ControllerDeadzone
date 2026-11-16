// Native hot-path InputAxis hook. Runs on the game thread, no Python work unless
// the KBM/controller owner or guarded-stick decision actually changes.

namespace axis_hook {
namespace hm = unrealsdk::hook_manager;
namespace u = unrealsdk::unreal;
constexpr wchar_t FN[] = L"WillowGame.WillowGameViewportClient:InputAxis";
constexpr wchar_t ID[] = L"ControllerDeadzoneControl:NativeAxis156";
bool installed=false;
bool enabled=false;
py::object signal{};
std::uint64_t hook_calls=0, decoded=0, unknown_axes=0, faults=0,
    python_notifications=0, unchanged=0;
int registration=0;
std::string last_error{};
bool guard_l=false,guard_r=false,have_guard=false;
int prior_owner=0;

int physical_axis(const u::FName& key) {
    static const u::FName left_x{L"XboxTypeS_LeftX"},left_y{L"XboxTypeS_LeftY"},
       right_x{L"XboxTypeS_RightX"},right_y{L"XboxTypeS_RightY"};
    if(key==left_x)return 0;
    if(key==left_y)return 1;
    if(key==right_x)return 2;
    if(key==right_y)return 3;
    return -1;
}

bool callback(hm::Details& hook) noexcept {
    ++hook_calls;
    if(!enabled||!hook.args)return false;
    try {
        static const u::FName key_name{L"Key"},delta_name{L"Delta"},
            gamepad_name{L"bGamepad"};
        const auto key=hook.args->get<u::ZNameProperty>(key_name);
        const auto delta=hook.args->get<u::ZFloatProperty>(delta_name);
        const bool gamepad=hook.args->get<u::ZBoolProperty>(gamepad_name);
        const int axis=physical_axis(key);
        if(gamepad&&axis<0)++unknown_axes;
        const int old_owner=static_cast<int>(backend.input.owner);
        const int owner=backend.input.axis(axis,delta,gamepad,monotonic_seconds());
        ++decoded;
        // Native guard hysteresis must be evaluated on BOTH zero and nonzero
        // stick events: release a temporary Speed=0 as soon as motion is real.
        auto guard=backend.input.guard(monotonic_seconds());
        const bool owner_changed=owner!=old_owner;
        const bool guard_changed=!have_guard||guard_l!=guard.first||guard_r!=guard.second;
        guard_l=guard.first;guard_r=guard.second;have_guard=true;
        if((owner_changed||guard_changed)&&!signal.is_none()) {
            py::gil_scoped_acquire gil{};
            signal(owner,guard.first,guard.second,owner_changed);
            ++python_notifications;
        } else ++unchanged;
    } catch(const std::exception& e) {
        ++faults;last_error=e.what();enabled=false; // fail closed
    } catch(...) {
        ++faults;last_error="unknown native InputAxis exception";enabled=false;
    }
    return false; // NEVER block engine input.
}

bool start(py::object receiver) {
    if(!PyCallable_Check(receiver.ptr()))return false;
    if(installed){ signal=std::move(receiver);enabled=true;return true; }
    if(hm::has_hook(FN,hm::Type::PRE,ID)){
        registration=-2;return false;
    }
    const bool reported=hm::add_hook(FN,hm::Type::PRE,ID,&callback);
    if(!hm::has_hook(FN,hm::Type::PRE,ID)){
        registration=-3;return false;
    }
    signal=std::move(receiver);
    installed=true;enabled=true;registration=reported?1:2;
    prior_owner=static_cast<int>(backend.input.owner);
    have_guard=false;
    return true;
}
bool stop() noexcept {
    enabled=false;
    // Release Python ref only if holding GIL (called from Python normally).
    if(Py_IsInitialized()&&PyGILState_Check())signal=py::none();
    have_guard=false;
    if(!installed)return false;
    try {
        const bool removed=hm::remove_hook(FN,hm::Type::PRE,ID);
        installed=hm::has_hook(FN,hm::Type::PRE,ID);
        return removed;
    }
    catch(...) {return false;}
}
bool active() noexcept {return installed&&enabled;}
void append_stats(py::dict& d) {
    d["native_axis_hook_active"]=active();
    d["native_axis_hook_calls"]=hook_calls;
    d["native_axis_hook_decoded"]=decoded;
    d["native_axis_hook_unmapped_controller_events"]=unknown_axes;
    d["native_axis_hook_faults"]=faults;
    d["native_axis_hook_python_notifications"]=python_notifications;
    d["native_axis_hook_unchanged_events"]=unchanged;
    d["native_axis_hook_registration"]=registration;
    d["native_axis_hook_error"]=last_error;
}
} // namespace axis_hook
