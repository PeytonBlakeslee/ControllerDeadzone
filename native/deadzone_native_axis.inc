// Native hot-path InputAxis hook. Runs on the game thread, no Python work unless
// the KBM/controller owner or guarded-stick decision actually changes.

namespace axis_hook {
namespace hm = unrealsdk::hook_manager;
namespace u = unrealsdk::unreal;
constexpr wchar_t FN[] = L"WillowGame.WillowGameViewportClient:InputAxis";
std::wstring ID;
bool installed=false;
bool pending_removal=false;
bool enabled=false;
py::object signal{};
bool guard_l=false,guard_r=false,have_guard=false;

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
    if(!enabled||!hook.args)return false;
    try {
        int axis;float delta;bool gamepad;
        {
            event_decode::axis.resolve(hook.args->type);
            const auto key=hook.args->get<u::ZNameProperty>(event_decode::axis.key);
            delta=hook.args->get<u::ZFloatProperty>(event_decode::axis.delta);
            gamepad=hook.args->get<u::ZBoolProperty>(event_decode::axis.gamepad);
            axis=physical_axis(key);
        }
        const int old_owner=static_cast<int>(backend.input.owner);
        int owner;std::pair<bool,bool> guard;
        // Native guard hysteresis must be evaluated on BOTH zero and nonzero
        // stick events: release a temporary Speed=0 as soon as motion is real.
        {
            const double now=monotonic_seconds();
            owner=backend.input.axis(axis,delta,gamepad,now);
            guard=backend.input.guard(now);
        }
        const bool owner_changed=owner!=old_owner;
        const bool guard_changed=!have_guard||guard_l!=guard.first||guard_r!=guard.second;
        guard_l=guard.first;guard_r=guard.second;have_guard=true;
        if((owner_changed||guard_changed)&&!signal.is_none()) {
            py::gil_scoped_acquire gil{};
            signal(owner,guard.first,guard.second,owner_changed);
        }
    } catch(...) {
        enabled=false; // fail closed
    }
    return false; // NEVER block engine input.
}

bool start(py::object receiver) {
    if(!PyCallable_Check(receiver.ptr()))return false;
    event_decode::axis.clear();
    if(pending_removal)return false;
    if(installed && !hm::has_hook(FN,hm::Type::PRE,ID.c_str()))installed=false;
    if(installed){ signal=std::move(receiver);enabled=true;return true; }
    ID=next_hook_id(L"ControllerDeadzoneControl:NativeAxis156");
    if(hm::has_hook(FN,hm::Type::PRE,ID.c_str())){
        return false;
    }
    installed=true;  // add may throw after insertion
    hm::add_hook(FN,hm::Type::PRE,ID.c_str(),&callback);
    if(!hm::has_hook(FN,hm::Type::PRE,ID.c_str())){
        installed=false;
        return false;
    }
    signal=std::move(receiver);
    installed=true;enabled=true;
    have_guard=false;
    return true;
}
bool stop() noexcept {
    enabled=false;
    event_decode::axis.clear();
    // Release Python ref only if holding GIL (called from Python normally).
    if(Py_IsInitialized()&&PyGILState_Check())signal=py::none();
    have_guard=false;
    if(!installed)return false;
    pending_removal=true;
    try {
        const bool removed=hm::remove_hook(FN,hm::Type::PRE,ID.c_str());
        // True confirms our removal. Do not query and retain a replacement.
        installed=!removed&&hm::has_hook(FN,hm::Type::PRE,ID.c_str());
        if(!installed)pending_removal=false;
        return removed;
    }
    catch(...) {return false;}
}
bool active() noexcept {return installed&&enabled;}
} // namespace axis_hook
