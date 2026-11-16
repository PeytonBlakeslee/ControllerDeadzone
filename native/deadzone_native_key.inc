// Native Win32 InputKey PRE callback for verified radial-only C++ sessions.
// Engine-side event parsing and held-key updates happen without Python.
// Python is notified only when device owner/guard state changes.
namespace key_hook {
namespace hm=unrealsdk::hook_manager;
namespace u=unrealsdk::unreal;
constexpr wchar_t FUNCTION[]=L"WillowGame.WillowGameViewportClient:InputKey";
std::wstring IDENTIFIER;
bool installed=false, enabled=false;
bool pending_removal=false;
py::object notification{};
bool have_guard=false, left_guard=false, right_guard=false;

bool callback(hm::Details& h) noexcept {
    if(!enabled || !h.args)return false;
    try {
        event_decode::key.resolve(h.args->type);
        const u::FName name=h.args->get<u::ZNameProperty>(event_decode::key.key);
        const auto event=h.args->get<u::ZByteProperty>(event_decode::key.event);
        const bool gamepad=h.args->get<u::ZBoolProperty>(event_decode::key.gamepad);
        const int kind=event==0?0:event==1?1:event==2?2:event==3?3:4;
        const int previous=static_cast<int>(backend.input.owner);
        const double now=monotonic_seconds();
        // The controller branch never examines a key name. Keep keyboard names
        // unchanged because held-key/mouse classification depends on them.
        const int current=backend.input.key(gamepad ? std::string{} : std::string{name},
                                            gamepad,kind,now);
        const auto guard=backend.input.guard(now);
        const bool changed=current!=previous;
        const bool guard_changed=!have_guard||left_guard!=guard.first||
                                   right_guard!=guard.second;
        have_guard=true;left_guard=guard.first;right_guard=guard.second;
        if((changed||guard_changed)&&!notification.is_none()) {
            py::gil_scoped_acquire gil{};
            notification(current,guard.first,guard.second,changed);
        }
    } catch(...) {
        enabled=false; // fail closed
    }
    return false; // Never intercept or block a game input.
}

bool start(py::object receiver) {
    if(!PyCallable_Check(receiver.ptr()))return false;
    event_decode::key.clear();
    if(pending_removal)return false;
    if(installed && !hm::has_hook(FUNCTION,hm::Type::PRE,IDENTIFIER.c_str()))installed=false;
    if(installed){notification=std::move(receiver);enabled=true;return true;}
    IDENTIFIER=next_hook_id(L"ControllerDeadzoneControl:NativeKey157");
    if(hm::has_hook(FUNCTION,hm::Type::PRE,IDENTIFIER.c_str())){
        return false;
    }
    installed=true;  // add may throw after insertion
    hm::add_hook(FUNCTION,hm::Type::PRE,IDENTIFIER.c_str(),&callback);
    if(!hm::has_hook(FUNCTION,hm::Type::PRE,IDENTIFIER.c_str())){
        installed=false;
        return false;
    }
    notification=std::move(receiver);
    have_guard=false;
    installed=true;enabled=true;
    return true;
}
bool stop() noexcept {
    enabled=false;
    event_decode::key.clear();
    if(Py_IsInitialized()&&PyGILState_Check())notification=py::none();
    have_guard=false;
    if(!installed)return false;
    pending_removal=true;
    try{
        const bool removed=hm::remove_hook(FUNCTION,hm::Type::PRE,IDENTIFIER.c_str());
        // True confirms our removal. Do not query and retain a replacement.
        installed=!removed&&hm::has_hook(FUNCTION,hm::Type::PRE,IDENTIFIER.c_str());
        if(!installed)pending_removal=false;
        return removed;
    }
    catch(...){return false;}
}
bool active() noexcept {return installed&&enabled;}
} // namespace key_hook
