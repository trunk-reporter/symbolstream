add_library(symbolstream MODULE symbolstream.cc)

target_include_directories(symbolstream PRIVATE
    ${PROJECT_SOURCE_DIR}
)

target_link_libraries(symbolstream
    trunk_recorder_library
    Boost::log
)

install(TARGETS symbolstream LIBRARY DESTINATION ${CMAKE_INSTALL_PREFIX}/lib/trunk-recorder)
