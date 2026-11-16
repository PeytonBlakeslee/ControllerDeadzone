// v1.6.1: direct native discovery of live physical controller input bindings.
// This method does not modify engine state or retain UObject pointers.
// Python passes a freshly verified LOCAL WillowPlayerInput address.
namespace binding_discovery {
namespace u=unrealsdk::unreal;
constexpr size_t MAX_BINDINGS=1024;
constexpr size_t MAX_PHYSICAL_AXES=256;

py::tuple scan(uintptr_t pi_addr) {
  py::list result;
  py::list conflicting;
  try {
    if(!pi_addr || sizeof(void*)!=4)throw std::runtime_error("no verified Win32 PlayerInput");
    auto* pi=reinterpret_cast<u::UObject*>(pi_addr);
    auto axes=pi->get<u::ZArrayProperty>(u::FName{L"Axes"});
    if(axes.size()>MAX_BINDINGS)throw std::runtime_error("unexpected PlayerInput Axes size");
    struct Found {uintptr_t ptr=0;int index=-1;std::string logical{};};
    std::array<Found,4> entries{};
    std::array<bool,4> ambiguous{};
    const u::FName axis_name{L"AxisName"};
    const u::FName handler_name{L"InputDeviceHandler"};
    const u::FName device_name{L"SourceDevice"};
    const u::FName index_name{L"SourceIndex"};
    for(size_t n=0;n<axes.size();++n) {
      try {
        auto row=axes.get_at<u::ZStructProperty>(n);
        auto handler=row.get<u::ZStructProperty>(handler_name);
        auto* device=handler.get<u::ZObjectProperty>(device_name);
        if(!device || device->Class()->Name()!=u::FName{L"InputDeviceDefinition"} ||
             device->Name()!=u::FName{L"InputDevice_Controller"})continue;
        const int idx=handler.get<u::ZIntProperty>(index_name);
        if(idx<0)continue;
        auto physical_rows=device->get<u::ZArrayProperty>(u::FName{L"Axes"});
        if(physical_rows.size()>MAX_PHYSICAL_AXES ||
           static_cast<size_t>(idx)>=physical_rows.size())continue;
        auto physical_row=physical_rows.get_at<u::ZStructProperty>(static_cast<size_t>(idx));
        const u::FName physical=physical_row.get<u::ZNameProperty>(axis_name);
        int slot=-1;
        for(int x=0;x<4;++x) {
           if(physical==u::FName{axis_fields::PHYSICAL[x]}) {slot=x;break;}
        }
        if(slot<0)continue;
        auto& entry=entries[static_cast<size_t>(slot)];
        const auto addr=reinterpret_cast<uintptr_t>(device);
        const auto logical=std::string{row.get<u::ZNameProperty>(axis_name)};
        if(entry.ptr && (entry.ptr!=addr || entry.index!=idx || entry.logical!=logical)) {
           ambiguous[static_cast<size_t>(slot)]=true;
        } else {
           entry.ptr=addr;
           entry.index=idx;
           entry.logical=logical;
        }
      }catch(const std::exception&) {
        // Mixed non-controller or obsolete binding rows may be malformed.
        // Skip only this row; do not invent an axis mapping.
      }
    }
    for(int i=0;i<4;++i) {
      if(ambiguous[static_cast<size_t>(i)]) {conflicting.append(i);continue;}
      const auto& e=entries[static_cast<size_t>(i)];
      if(e.ptr)result.append(py::make_tuple(i,e.ptr,e.index,e.logical));
    }
    return py::make_tuple(1,result,conflicting);
  }catch(...) { /* return an unavailable result to Python */ }
  return py::make_tuple(0,result,conflicting);
}
} // namespace binding_discovery
