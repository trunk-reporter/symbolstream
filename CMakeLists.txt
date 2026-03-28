add_library(symbolstream MODULE symbolstream.cc)

include_directories(
  ${CMAKE_BINARY_DIR}/../
)

target_link_libraries(symbolstream
  trunk_recorder_library
  ${Boost_LIBRARIES}
  Boost::log
  ${GNURADIO_PMT_LIBRARIES}
  ${GNURADIO_RUNTIME_LIBRARIES}
  ${LIBOP25_REPEATER_LIBRARIES}
  gnuradio-op25_repeater
)

if(NOT Gnuradio_VERSION VERSION_LESS "3.8")
  target_link_libraries(symbolstream
    gnuradio::gnuradio-analog
    gnuradio::gnuradio-blocks
    gnuradio::gnuradio-digital
    gnuradio::gnuradio-filter
    gnuradio::gnuradio-pmt
  )
endif()

install(TARGETS symbolstream LIBRARY DESTINATION ${CMAKE_INSTALL_PREFIX}/lib/trunk-recorder)
